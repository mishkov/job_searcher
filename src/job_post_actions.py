import asyncio

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, MessageEntity

from src.error_reporting import ErrorReporter, report_error
from src.storage.job_post_repository import JobPostRepository, SavedJobPost


ARCHIVE_POST = 'job:archive'
RESTORE_POST = 'job:restore'


def job_post_keyboard(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='Open job 🔗', url=url)],
        [InlineKeyboardButton(text='✅ Applied', callback_data=ARCHIVE_POST)],
    ])


class JobPostActions:
    def __init__(self, repository: JobPostRepository, reporter: ErrorReporter):
        self.repository = repository
        self.reporter = reporter
        # Serialize edits so overlapping archive/restore clicks finish in order.
        self._lock = asyncio.Lock()

    async def handle(self, query: CallbackQuery) -> None:
        message = query.message
        if (not isinstance(message, Message) or message.chat.type != 'private'
                or query.from_user.id != message.chat.id):
            await query.answer('Use these buttons in the original private chat.', show_alert=True)
            return
        if query.data not in (ARCHIVE_POST, RESTORE_POST):
            await query.answer()
            return

        async with self._lock:
            try:
                post = await self.repository.get(message.chat.id, message.message_id)
                if query.data == ARCHIVE_POST:
                    if post is None:
                        prefix = '🧑‍💻 Title: '
                        if not message.text or not message.text.startswith(prefix) or not message.reply_markup:
                            await query.answer('This message cannot be archived.', show_alert=True)
                            return
                        post = await self.repository.save(SavedJobPost(
                            chat_id=message.chat.id,
                            message_id=message.message_id,
                            title=message.text.removeprefix(prefix).partition('\n\n🏝️ Remote allowed: ')[0],
                            text=message.text,
                            entities=[entity.model_dump(mode='json', exclude_none=True)
                                      for entity in message.entities or []],
                            reply_markup=message.reply_markup.model_dump(mode='json', exclude_none=True),
                        ))
                    markup = InlineKeyboardMarkup.model_validate(post.reply_markup)
                    for row in markup.inline_keyboard:
                        for button in row:
                            if button.callback_data == ARCHIVE_POST:
                                button.text = 'Restore post'
                                button.callback_data = RESTORE_POST
                    text = f'✅ Applied [{post.title}]'
                    entities = []
                else:
                    if post is None:
                        await query.answer('The saved post could not be found.', show_alert=True)
                        return
                    text = post.text
                    entities = [MessageEntity.model_validate(entity) for entity in post.entities]
                    markup = InlineKeyboardMarkup.model_validate(post.reply_markup)

                try:
                    await message.edit_text(text, parse_mode=None, entities=entities, reply_markup=markup)
                except TelegramBadRequest as error:
                    if 'message is not modified' not in error.message.lower():
                        raise
            except Exception as error:
                report_error(self.reporter, error, operation='archive_job_post' if query.data == ARCHIVE_POST
                             else 'restore_job_post', chat_id=message.chat.id, message_id=message.message_id)
                await query.answer('Could not update the post. Please try again.', show_alert=True)
                return
        await query.answer()
