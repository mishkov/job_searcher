import asyncio
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import EditMessageText
from aiogram.types import CallbackQuery, Chat, InaccessibleMessage, Message, MessageEntity, User
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from src.job_post_actions import ARCHIVE_POST, RESTORE_POST, JobPostActions, job_post_keyboard
from src.storage.job_post_repository import SavedJobPost
from src.storage.mysql.base import Base
from src.storage.mysql.job_post_repository import JobPostModel, MysqlJobPostRepository
from src.storage.mysql.seen_job_model import SeenJobModel


TITLE = 'Senior [Python] 🐍 *Developer*'
TEXT = (f'🧑‍💻 Title: {TITLE}\n\n🏝️ Remote allowed: True\n\n'
        '💸 Salary: $100,000\n\n📜 A displayed excerpt...')
NOW = datetime(2026, 10, 26, tzinfo=timezone.utc)


class JobPostTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database_url = f'sqlite:///{Path(directory.name) / "posts.sqlite3"}'
        self.engine = create_engine(self.database_url)
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.repository = MysqlJobPostRepository(self.engine)
        self.reporter = Mock()
        self.actions = JobPostActions(self.repository, self.reporter)
        self.message = Message(
            message_id=42, date=NOW, chat=Chat(id=1234567890123, type='private'), text=TEXT,
            entities=[MessageEntity(type='bold', offset=6, length=6),
                      MessageEntity(type='italic', offset=0, length=5)],
            reply_markup=job_post_keyboard('https://example.com/job'),
        )
        self.edit = self.patch(Message, 'edit_text', new=AsyncMock(return_value=True))
        self.answer = self.patch(CallbackQuery, 'answer', new=AsyncMock())

    def patch(self, *args, **kwargs):
        patcher = patch.object(*args, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def query(self, action=ARCHIVE_POST, message=None):
        return CallbackQuery(id='query', from_user=User(id=self.message.chat.id, is_bot=False, first_name='Owner'),
                             chat_instance='chat', data=action, message=message or self.message)

    async def saved(self):
        return await self.repository.get(self.message.chat.id, self.message.message_id)

    def edited_message(self):
        call = self.edit.await_args
        return self.message.model_copy(update={
            'text': call.args[0], 'entities': call.kwargs['entities'],
            'reply_markup': call.kwargs['reply_markup'],
        })

    async def test_archive_saves_original_before_edit_and_keeps_link(self):
        async def edit(*args, **kwargs):
            self.assertEqual((await self.saved()).text, TEXT)
        self.edit.side_effect = edit
        await self.actions.handle(self.query())
        saved = await self.saved()
        self.assertEqual(saved.title, TITLE)
        self.assertEqual(saved.entities, [entity.model_dump(mode='json', exclude_none=True)
                                          for entity in self.message.entities])
        self.assertEqual(saved.reply_markup, self.message.reply_markup.model_dump(mode='json', exclude_none=True))
        self.assertEqual(self.edit.await_args.args, (f'✅ Applied [{TITLE}]',))
        self.assertIsNone(self.edit.await_args.kwargs['parse_mode'])
        self.assertEqual(self.edit.await_args.kwargs['entities'], [])
        markup = self.edit.await_args.kwargs['reply_markup']
        self.assertEqual(markup.inline_keyboard[0][0].url, 'https://example.com/job')
        self.assertEqual(markup.inline_keyboard[1][0].text, 'Restore post')
        self.assertEqual(markup.inline_keyboard[1][0].callback_data, RESTORE_POST)
        self.answer.assert_awaited_once_with()

    async def test_restore_after_restart_reproduces_text_entities_and_buttons(self):
        await self.actions.handle(self.query())
        archived = self.edited_message()
        self.engine.dispose()
        restarted_engine = create_engine(self.database_url)
        self.addCleanup(restarted_engine.dispose)
        restarted = JobPostActions(MysqlJobPostRepository(restarted_engine), self.reporter)
        self.edit.reset_mock()
        await restarted.handle(self.query(RESTORE_POST, archived))
        self.edit.assert_awaited_once_with(TEXT, parse_mode=None, entities=self.message.entities,
                                          reply_markup=self.message.reply_markup)
        self.assertIsNotNone(await self.saved())

    async def test_repeated_archive_restore_preserves_first_snapshot_and_seen_history(self):
        with Session(self.engine) as session:
            session.add(SeenJobModel(job_fingerprint='already-delivered'))
            session.commit()
        for _ in range(3):
            await self.actions.handle(self.query())
            archived = self.edited_message()
            # A delayed duplicate archive callback can contain the collapsed text.
            await self.actions.handle(self.query(ARCHIVE_POST, archived))
            self.assertEqual((await self.saved()).text, TEXT)
            await self.actions.handle(self.query(RESTORE_POST, archived))
            self.assertEqual(self.edit.await_args.args, (TEXT,))
        with Session(self.engine) as session:
            self.assertEqual(session.scalars(select(SeenJobModel.job_fingerprint)).all(), ['already-delivered'])
            self.assertEqual(len(session.scalars(select(JobPostModel)).all()), 1)

    async def test_overlapping_archive_and_restore_finish_in_order(self):
        archive_edit_started = asyncio.Event()
        release_edit = asyncio.Event()
        async def edit(text, **kwargs):
            if text.startswith('✅ Applied'):
                archive_edit_started.set()
                await release_edit.wait()
        self.edit.side_effect = edit
        archive = asyncio.create_task(self.actions.handle(self.query()))
        await archive_edit_started.wait()
        restore = asyncio.create_task(self.actions.handle(self.query(RESTORE_POST)))
        release_edit.set()
        await asyncio.gather(archive, restore)
        self.assertEqual([call.args[0] for call in self.edit.await_args_list], [f'✅ Applied [{TITLE}]', TEXT])

    async def test_database_write_failure_keeps_original_message(self):
        self.patch(self.repository, 'save', new=AsyncMock(side_effect=RuntimeError('database unavailable')))
        await self.actions.handle(self.query())
        self.edit.assert_not_awaited()
        self.assertIsNone(await self.saved())
        self.answer.assert_awaited_once_with('Could not update the post. Please try again.', show_alert=True)
        self.assertEqual(self.reporter.report.call_args.kwargs['context']['operation'], 'archive_job_post')

    async def test_database_read_failure_keeps_message_and_reports_error(self):
        self.patch(self.repository, 'get', new=AsyncMock(side_effect=RuntimeError('database unavailable')))
        await self.actions.handle(self.query(RESTORE_POST))
        self.edit.assert_not_awaited()
        self.answer.assert_awaited_once_with('Could not update the post. Please try again.', show_alert=True)
        self.assertEqual(self.reporter.report.call_args.kwargs['context']['operation'], 'restore_job_post')

    async def test_failed_edit_keeps_snapshot_for_retry(self):
        self.edit.side_effect = TelegramNetworkError(method=EditMessageText(text='unused'), message='timeout')
        await self.actions.handle(self.query())
        self.assertEqual((await self.saved()).text, TEXT)
        self.answer.assert_awaited_once_with('Could not update the post. Please try again.', show_alert=True)
        self.edit.side_effect = None
        await self.actions.handle(self.query())
        await self.actions.handle(self.query(RESTORE_POST, self.edited_message()))
        self.assertEqual(self.edit.await_args.args, (TEXT,))

    async def test_failed_restore_can_be_retried(self):
        await self.actions.handle(self.query())
        archived = self.edited_message()
        self.edit.side_effect = TelegramBadRequest(method=EditMessageText(text='unused'), message='message not found')
        await self.actions.handle(self.query(RESTORE_POST, archived))
        self.assertEqual((await self.saved()).text, TEXT)
        self.assertEqual(self.answer.await_args.kwargs, {'show_alert': True})
        self.edit.side_effect = None
        await self.actions.handle(self.query(RESTORE_POST, archived))
        self.assertEqual(self.edit.await_args.args, (TEXT,))

    async def test_duplicate_click_message_not_modified_is_success(self):
        await self.actions.handle(self.query())
        self.edit.side_effect = TelegramBadRequest(method=EditMessageText(text='unused'),
                                                   message='Bad Request: message is not modified')
        for action in (ARCHIVE_POST, RESTORE_POST):
            await self.actions.handle(self.query(action))
        self.reporter.report.assert_not_called()
        self.answer.assert_awaited_with()

    async def test_missing_snapshot_alerts_without_editing(self):
        await self.actions.handle(self.query(RESTORE_POST))
        self.edit.assert_not_awaited()
        self.answer.assert_awaited_once_with('The saved post could not be found.', show_alert=True)

    async def test_group_other_user_and_inaccessible_messages_are_rejected(self):
        messages = [
            self.message.model_copy(update={'chat': Chat(id=-123, type='group')}),
            self.message.model_copy(update={'chat': Chat(id=999, type='private')}),
            InaccessibleMessage(chat=self.message.chat, message_id=42, date=0),
        ]
        for message in messages:
            with self.subTest(message=type(message).__name__):
                await self.actions.handle(self.query(message=message))
                self.assertTrue(self.answer.await_args.kwargs['show_alert'])
        self.edit.assert_not_awaited()
        self.assertIsNone(await self.saved())

    async def test_missing_message_is_rejected(self):
        query = self.query().model_copy(update={'message': None})
        await self.actions.handle(query)
        self.edit.assert_not_awaited()
        self.assertTrue(self.answer.await_args.kwargs['show_alert'])

    async def test_invalid_post_cannot_replace_a_missing_snapshot(self):
        message = self.message.model_copy(update={'text': '✅ Applied [Title]'})
        await self.actions.handle(self.query(message=message))
        self.edit.assert_not_awaited()
        self.assertIsNone(await self.saved())
        self.answer.assert_awaited_once_with('This message cannot be archived.', show_alert=True)

    async def test_repository_keeps_first_snapshot_and_scopes_by_chat_and_message(self):
        first = SavedJobPost(self.message.chat.id, 42, TITLE, TEXT, [], {})
        self.assertEqual(await self.repository.save(first), first)
        self.assertEqual(await self.repository.save(replace(first, text='collapsed')), first)
        for other in (replace(first, chat_id=999, text='other chat'),
                      replace(first, message_id=43, text='other message')):
            await self.repository.save(other)
            self.assertEqual(await self.repository.get(other.chat_id, other.message_id), other)
        self.assertEqual(await self.saved(), first)


if __name__ == '__main__':
    unittest.main()
