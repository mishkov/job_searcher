import asyncio
import importlib.util
import os
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from aiogram import Bot

from src.job import Job
from src.job_sources.andersen.andersen import AndersenFetchError, AndersenJobsSourceConfig
from src.job_sources.brightstar_lottery.brightstar_lottery import (
    BrightstarLotteryFetchError, BrightstarLotteryJobsSourceConfig,
)
from src.job_sources.smartrecruiters.smartrecruiters import SmartRecruitersFetchError, SmartRecruitersJobsSourceConfig
from src.job_sources.job_source import JobsSourceConfig
from src.job_sources.remote_ok.remote_ok import RemoteOkFetchError, RemoteOkJobsSourceConfig
from src.search_filter import SearchFilter
from src.monitoring import MonitoringState, SearchResult, SleepPeriod, VacancyMonitor
from src.storage.mysql.monitoring_store import MysqlMonitoringStore
from src.storage.mysql.threaded_history_repository import ThreadedHistoryRepository


CUTOFF = datetime(2026, 10, 12, tzinfo=timezone.utc)


class MainTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = types.ModuleType('config')
        self.config.SOURCES = [RemoteOkJobsSourceConfig(SearchFilter(keywords=['flutter']))]
        spec = importlib.util.spec_from_file_location('testable_main', Path(__file__).resolve().parents[1] / 'main.py')
        self.main = importlib.util.module_from_spec(spec)
        # Load wiring without reading credentials, opening a session, or running the bot.
        with patch.dict(sys.modules, {'config': self.config}), patch.dict(os.environ, {
            'IS_DEBUG_MODE': '', 'PATH_TO_TELEGRAM_SESSION': 'unused',
            'TELEGRAM_APP_API_ID': '1', 'TELEGRAM_APP_API_HASH': 'unused',
            'DATABASE_URL': 'unused', 'CACHE_PATH': '',
        }), patch('dotenv.load_dotenv'), patch('telethon.TelegramClient'), patch('sqlalchemy.create_engine'):
            spec.loader.exec_module(self.main)
        self.report = self.patch_object(self.main.error_reporter, 'report')
        self.main.client = MagicMock()
        self.repository = Mock(spec=ThreadedHistoryRepository, get_last_search_time=AsyncMock(return_value=CUTOFF),
                               save_last_search_time=AsyncMock(), seen=AsyncMock(return_value=False),
                               add_seen_job=AsyncMock())
        self.patch_object(self.main, 'ThreadedHistoryRepository', return_value=self.repository)
        self.source = Mock(config=self.config.SOURCES[0], fetch=AsyncMock(return_value=[]))
        self.factory = self.patch_object(self.main, 'RemoteOkJobsSource', return_value=self.source)
        self.status = Mock(edit_text=AsyncMock())
        self.message = Mock(answer=AsyncMock(return_value=self.status))

    def patch_object(self, *args, **kwargs):
        patcher = patch.object(*args, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()


class MainReportingTests(MainTestCase):
    async def test_stop_finishes_current_source_and_does_not_touch_later_history(self):
        self.config.SOURCES.append(RemoteOkJobsSourceConfig(SearchFilter(keywords=['python'])))
        proceed = True
        job = Job('Title', 'Description', '-', None, 'https://example.com/1', 'one')
        async def fetch(since):
            nonlocal proceed
            proceed = False
            return [job]
        self.source.fetch.side_effect = fetch
        result = await self.main.search_jobs(self.message.answer, lambda: proceed)
        self.assertTrue(result.interrupted)
        self.assertEqual(result.delivered, 1)
        self.message.answer.assert_awaited_once()
        self.repository.add_seen_job.assert_awaited_once_with(job)
        self.repository.save_last_search_time.assert_awaited_once()
        self.repository.get_last_search_time.assert_awaited_once()

    async def test_no_source_or_history_touched_when_already_paused(self):
        result = await self.main.search_jobs(self.message.answer, lambda: False)
        self.assertTrue(result.interrupted)
        self.source.fetch.assert_not_awaited()
        self.repository.get_last_search_time.assert_not_awaited()
        self.message.answer.assert_not_awaited()

    async def test_known_source_failure_is_reported_once_and_next_source_runs(self):
        self.config.SOURCES.append(RemoteOkJobsSourceConfig(SearchFilter(keywords=['python'])))
        next_source = Mock(config=self.config.SOURCES[1], fetch=AsyncMock(return_value=[]))
        self.factory.side_effect = [self.source, next_source]
        error = RemoteOkFetchError('request failed')
        self.source.fetch.side_effect = error
        result = await self.main.search_jobs(self.message.answer)
        self.report.assert_called_once()
        self.assertIs(self.report.call_args.args[0], error)
        self.assertEqual(self.report.call_args.kwargs['context']['filters']['keywords'], ['flutter'])
        next_source.fetch.assert_awaited_once()
        self.assertEqual(result.failed_sources, 1)
        self.message.answer.assert_not_awaited()

    async def test_unexpected_source_failure_propagates_with_one_report(self):
        error = ValueError('unexpected data')
        self.source.fetch.side_effect = error
        with self.assertRaises(ValueError) as raised:
            await self.main.search_jobs(self.message.answer)
        self.assertIs(raised.exception, error)
        self.report.assert_called_once()
        self.assertEqual(self.report.call_args.kwargs['context']['operation'], 'fetch_jobs')

    async def test_andersen_wiring_and_successful_search_checkpoint(self):
        config = AndersenJobsSourceConfig(SearchFilter(['flutter']))
        self.config.SOURCES = [config]
        source = Mock(config=config, fetch=AsyncMock(return_value=[]))
        factory = self.patch_object(self.main, 'AndersenJobsSource', return_value=source)
        await self.main.search_jobs(self.message.answer)
        factory.assert_called_once_with(config)
        source.fetch.assert_awaited_once_with(CUTOFF)
        self.repository.save_last_search_time.assert_awaited_once()
        self.assertEqual(self.repository.save_last_search_time.call_args.args[0], config.history_key)

    async def test_smartrecruiters_wiring_and_successful_checkpoint(self):
        config = SmartRecruitersJobsSourceConfig(SearchFilter(['flutter']))
        self.config.SOURCES = [config]
        source = Mock(config=config, fetch=AsyncMock(return_value=[]))
        factory = self.patch_object(self.main, 'SmartRecruitersJobsSource', return_value=source)
        await self.main.search_jobs(self.message.answer)
        factory.assert_called_once_with(config)
        source.fetch.assert_awaited_once_with(CUTOFF)
        self.repository.save_last_search_time.assert_awaited_once()
        self.assertEqual(self.repository.save_last_search_time.call_args.args[0], config.history_key)

    async def test_brightstar_lottery_wiring_and_successful_checkpoint(self):
        config = BrightstarLotteryJobsSourceConfig(SearchFilter(['flutter']))
        self.config.SOURCES = [config]
        source = Mock(config=config, fetch=AsyncMock(return_value=[]))
        factory = self.patch_object(self.main, 'BrightstarLotteryJobsSource', return_value=source)
        result = await self.main.search_jobs(self.message.answer)
        factory.assert_called_once_with(config)
        source.fetch.assert_awaited_once_with(CUTOFF)
        self.repository.save_last_search_time.assert_awaited_once()
        self.assertEqual(self.repository.save_last_search_time.call_args.args[0], config.history_key)
        self.assertEqual(result.failed_sources, 0)

    async def test_brightstar_lottery_failure_keeps_checkpoint_and_continues(self):
        config = BrightstarLotteryJobsSourceConfig(SearchFilter(['flutter']))
        self.config.SOURCES.insert(0, config)
        error = BrightstarLotteryFetchError('Brightstar Lottery returned HTTP 500')
        source = Mock(config=config, fetch=AsyncMock(side_effect=error))
        self.patch_object(self.main, 'BrightstarLotteryJobsSource', return_value=source)
        result = await self.main.search_jobs(self.message.answer)
        self.report.assert_called_once()
        self.assertIs(self.report.call_args.args[0], error)
        self.assertEqual(self.report.call_args.kwargs['context']['source'], 'Brightstar Lottery')
        self.source.fetch.assert_awaited_once()
        self.repository.save_last_search_time.assert_awaited_once()
        self.assertEqual(self.repository.save_last_search_time.call_args.args[0], self.source.config.history_key)
        self.assertEqual(result.failed_sources, 1)
        self.message.answer.assert_not_awaited()

    async def test_smartrecruiters_failure_keeps_checkpoint_and_continues(self):
        config = SmartRecruitersJobsSourceConfig(SearchFilter(['flutter']))
        self.config.SOURCES.insert(0, config)
        error = SmartRecruitersFetchError('SmartRecruiters API returned HTTP 500')
        source = Mock(config=config, fetch=AsyncMock(side_effect=error))
        self.patch_object(self.main, 'SmartRecruitersJobsSource', return_value=source)
        await self.main.search_jobs(self.message.answer)
        self.report.assert_called_once()
        self.assertIs(self.report.call_args.args[0], error)
        self.assertEqual(self.report.call_args.kwargs['context']['source'], 'SmartRecruiters')
        self.source.fetch.assert_awaited_once()
        self.repository.save_last_search_time.assert_awaited_once()
        self.assertEqual(self.repository.save_last_search_time.call_args.args[0], self.source.config.history_key)

    async def test_andersen_failure_keeps_checkpoint_and_next_source_runs(self):
        config = AndersenJobsSourceConfig(SearchFilter(['flutter']))
        self.config.SOURCES.insert(0, config)
        error = AndersenFetchError('Andersen API returned HTTP 500')
        source = Mock(config=config, fetch=AsyncMock(side_effect=error))
        self.patch_object(self.main, 'AndersenJobsSource', return_value=source)
        await self.main.search_jobs(self.message.answer)
        self.report.assert_called_once()
        self.assertIs(self.report.call_args.args[0], error)
        self.assertEqual(self.report.call_args.kwargs['context']['source'], 'Andersen')
        self.source.fetch.assert_awaited_once()
        self.repository.save_last_search_time.assert_awaited_once()
        self.assertEqual(self.repository.save_last_search_time.call_args.args[0], self.source.config.history_key)

    async def test_job_delivery_and_history_failures_include_posting(self):
        job = Job('Flutter Engineer', 'Private posting copy', '-', True,
                  'https://example.com/job?tracking=secret', 'job-fingerprint')
        self.source.fetch.return_value = [job]
        for operation in ('check_seen_job', 'send_job', 'save_seen_job'):
            with self.subTest(operation=operation):
                self.report.reset_mock()
                self.message.answer.side_effect = None
                self.repository.seen.side_effect = None
                self.repository.add_seen_job.side_effect = None
                error = RuntimeError(operation)
                if operation == 'check_seen_job':
                    self.repository.seen.side_effect = error
                elif operation == 'send_job':
                    self.message.answer.side_effect = error
                else:
                    self.repository.add_seen_job.side_effect = error
                with self.assertRaises(RuntimeError):
                    await self.main.search_jobs(self.message.answer)
                self.report.assert_called_once()
                context = self.report.call_args.kwargs['context']
                self.assertEqual(context['operation'], operation)
                self.assertEqual(context['source'], 'Remote OK')
                self.assertEqual(context['since'], CUTOFF.isoformat())
                self.assertEqual(context['posting'], {'title': 'Flutter Engineer',
                                                     'url': 'https://example.com/job',
                                                     'fingerprint': 'job-fingerprint'})

    async def test_history_read_failure_is_reported_before_source_fetch(self):
        self.repository.get_last_search_time.side_effect = RuntimeError('database unavailable')
        with self.assertRaises(RuntimeError):
            await self.main.search_jobs(self.message.answer)
        self.assertEqual(self.report.call_args.kwargs['context']['operation'], 'read_search_history')
        self.source.fetch.assert_not_awaited()

    async def test_startup_database_failure_is_reported_and_propagates(self):
        error = RuntimeError('schema unavailable')
        with self.patch_object(self.main.Base.metadata, 'create_all', side_effect=error):
            with self.assertRaises(RuntimeError) as raised:
                await self.main.main()
        self.assertIs(raised.exception, error)
        self.report.assert_called_once_with(error, context={'operation': 'initialize_database'})

    async def test_unknown_source_configuration_has_accurate_source_context(self):
        self.config.SOURCES = [JobsSourceConfig('unknown', SearchFilter())]
        with self.assertRaisesRegex(ValueError, 'JobsSourceConfig'):
            await self.main.search_jobs(self.message.answer)
        context = self.report.call_args.kwargs['context']
        self.assertEqual(context['source'], 'unknown')
        self.assertEqual(context['operation'], 'create_source')


    async def test_empty_success_saves_utc_fetch_start(self):
        started_at = datetime(2026, 10, 14, 10, tzinfo=timezone.utc)
        with patch.object(self.main, 'datetime', wraps=datetime) as clock:
            clock.now.return_value = started_at
            await self.main.search_jobs(self.message.answer)
        key = self.config.SOURCES[0].history_key
        self.repository.get_last_search_time.assert_awaited_once_with(key)
        self.repository.save_last_search_time.assert_awaited_once_with(key, started_at)

    async def test_failed_source_keeps_checkpoint_and_successful_source_advances(self):
        next_config = RemoteOkJobsSourceConfig(SearchFilter(keywords=['python']))
        self.config.SOURCES.append(next_config)
        next_source = Mock(config=next_config, fetch=AsyncMock(return_value=[]))
        self.factory.side_effect = [self.source, next_source]
        self.source.fetch.side_effect = RemoteOkFetchError('request failed')
        next_cutoff = CUTOFF - timedelta(days=1)
        self.repository.get_last_search_time.side_effect = [CUTOFF, next_cutoff]
        await self.main.search_jobs(self.message.answer)
        self.source.fetch.assert_awaited_once_with(CUTOFF)
        next_source.fetch.assert_awaited_once_with(next_cutoff)
        self.repository.save_last_search_time.assert_awaited_once()
        self.assertEqual(self.repository.save_last_search_time.call_args.args[0], next_config.history_key)

    async def test_unexpected_fetch_failure_does_not_advance_checkpoint(self):
        self.source.fetch.side_effect = ValueError('invalid posting')
        with self.assertRaises(ValueError):
            await self.main.search_jobs(self.message.answer)
        self.repository.save_last_search_time.assert_not_awaited()

    async def test_partial_delivery_retries_old_interval_and_skips_delivered_jobs(self):
        first = Job('First', 'Description', '-', None, 'https://example.com/1', 'first')
        second = Job('Second', 'Description', '-', None, 'https://example.com/2', 'second')
        self.source.fetch.return_value = [first, second]
        seen = set()
        self.repository.seen.side_effect = lambda job: job.fingerprint in seen
        self.repository.add_seen_job.side_effect = lambda job: seen.add(job.fingerprint)
        self.message.answer.side_effect = [None, RuntimeError('delivery failed')]
        with self.assertRaises(RuntimeError):
            await self.main.search_jobs(self.message.answer)
        self.repository.save_last_search_time.assert_not_awaited()
        self.assertEqual(seen, {'first'})
        self.message.answer.reset_mock(side_effect=True)
        self.message.answer.return_value = self.status
        await self.main.search_jobs(self.message.answer)
        self.assertEqual(self.source.fetch.await_count, 2)
        self.assertTrue(all(call.args[0] == CUTOFF for call in self.source.fetch.call_args_list))
        self.assertEqual(seen, {'first', 'second'})
        self.assertEqual(self.repository.add_seen_job.await_count, 2)
        self.repository.save_last_search_time.assert_awaited_once()
        deliveries = [call for call in self.message.answer.call_args_list if 'reply_markup' in call.kwargs]
        self.assertEqual(len(deliveries), 1)
        self.assertIn('Second', deliveries[0].args[0])

    async def test_seen_record_failure_does_not_advance_checkpoint(self):
        self.source.fetch.return_value = [Job('Title', 'Description', '-', None, 'https://example.com/1', 'one')]
        self.repository.add_seen_job.side_effect = RuntimeError('database write failed')
        with self.assertRaises(RuntimeError):
            await self.main.search_jobs(self.message.answer)
        self.repository.save_last_search_time.assert_not_awaited()

    async def test_all_seen_jobs_still_advance_checkpoint(self):
        self.source.fetch.return_value = [Job('Title', 'Description', '-', None, 'https://example.com/1', 'one')]
        self.repository.seen.return_value = True
        await self.main.search_jobs(self.message.answer)
        self.repository.save_last_search_time.assert_awaited_once()
        self.repository.add_seen_job.assert_not_awaited()
        self.assertFalse(any('reply_markup' in call.kwargs for call in self.message.answer.call_args_list))

    async def test_first_search_uses_seven_day_fallback(self):
        started_at = datetime(2026, 10, 14, 10, tzinfo=timezone.utc)
        self.repository.get_last_search_time.return_value = None
        with patch.object(self.main, 'datetime', wraps=datetime) as clock:
            clock.now.return_value = started_at
            await self.main.search_jobs(self.message.answer)
        self.source.fetch.assert_awaited_once_with(started_at - timedelta(days=7))

    async def test_job_published_during_fetch_is_delivered_next_run(self):
        started_at = datetime(2026, 10, 14, 10, tzinfo=timezone.utc)
        completed_at = started_at + timedelta(minutes=5)
        during_fetch = started_at + timedelta(minutes=1)
        job = Job('New job', 'Description', '-', None, 'https://example.com/1', 'one')
        checkpoints = {self.config.SOURCES[0].history_key: CUTOFF}
        self.repository.get_last_search_time.side_effect = lambda key: checkpoints.get(key)
        self.repository.save_last_search_time.side_effect = lambda key, value: checkpoints.update({key: value})
        with patch.object(self.main, 'datetime', wraps=datetime) as clock:
            clock.now.return_value = started_at

            async def fetch(since):
                if self.source.fetch.await_count == 1:
                    clock.now.return_value = completed_at
                    return []  # Listing was read before this job appeared.
                return [job] if since <= during_fetch else []

            self.source.fetch.side_effect = fetch
            await self.main.search_jobs(self.message.answer)
            self.assertEqual(checkpoints[self.config.SOURCES[0].history_key], started_at)
            await self.main.search_jobs(self.message.answer)
        self.repository.add_seen_job.assert_awaited_once_with(job)

    async def test_checkpoint_write_failure_propagates_with_context(self):
        self.repository.save_last_search_time.side_effect = RuntimeError('checkpoint write failed')
        with self.assertRaises(RuntimeError):
            await self.main.search_jobs(self.message.answer)
        self.assertEqual(self.report.call_args.kwargs['context']['operation'], 'save_search_time')
        self.assertEqual(self.report.call_args.kwargs['context']['source'], self.config.SOURCES[0].name)

    async def test_debug_search_does_not_read_or_write_history(self):
        self.main.IS_DEBUG_MODE = True
        self.source.fetch.return_value = [Job('Title', 'Description', '-', None, 'https://example.com/1', 'one')]
        await self.main.search_jobs(self.message.answer)
        self.repository.get_last_search_time.assert_not_awaited()
        self.repository.save_last_search_time.assert_not_awaited()
        self.repository.seen.assert_not_awaited()
        self.repository.add_seen_job.assert_not_awaited()

    async def test_overlapping_searches_are_serialized(self):
        fetching = asyncio.Event()
        release = asyncio.Event()

        async def fetch(since):
            fetching.set()
            await release.wait()
            return []

        self.source.fetch.side_effect = fetch
        first = asyncio.create_task(self.main.search_jobs(self.message.answer))
        second = None
        try:
            await asyncio.wait_for(fetching.wait(), timeout=2)
            second = asyncio.create_task(self.main.search_jobs(self.message.answer))
            await asyncio.sleep(0)
            self.assertEqual(self.source.fetch.await_count, 1)
            self.assertEqual(self.repository.get_last_search_time.await_count, 1)
            release.set()
            await asyncio.wait_for(asyncio.gather(first, second), timeout=2)
            self.assertEqual(self.source.fetch.await_count, 2)
            self.assertEqual(self.repository.save_last_search_time.await_count, 2)
        finally:
            release.set()
            await asyncio.gather(*[task for task in (first, second) if task is not None], return_exceptions=True)


    async def test_later_source_failure_preserves_earlier_success(self):
        failed_config = RemoteOkJobsSourceConfig(SearchFilter(keywords=['python']))
        self.config.SOURCES.append(failed_config)
        failed_source = Mock(config=failed_config, fetch=AsyncMock(side_effect=RemoteOkFetchError('request failed')))
        self.factory.side_effect = [self.source, failed_source]
        await self.main.search_jobs(self.message.answer)
        self.repository.save_last_search_time.assert_awaited_once()
        self.assertEqual(self.repository.save_last_search_time.call_args.args[0], self.config.SOURCES[0].history_key)

    async def test_failure_releases_search_lock_for_retry(self):
        self.source.fetch.side_effect = [ValueError('invalid posting'), []]
        with self.assertRaises(ValueError):
            await self.main.search_jobs(self.message.answer)
        await asyncio.wait_for(self.main.search_jobs(self.message.answer), timeout=2)
        self.repository.save_last_search_time.assert_awaited_once()

    def test_environment_debug_mode_boolean_parsing(self):
        for value, expected in [('', False), ('false', False), ('False', False), ('0', False),
                                ('true', True), ('1', True), (' YES ', True), ('on', True)]:
            with self.subTest(value=value):
                spec = importlib.util.spec_from_file_location('debug_test_main', Path(__file__).resolve().parents[1] / 'main.py')
                module = importlib.util.module_from_spec(spec)
                with patch.dict(sys.modules, {'config': self.config}), patch.dict(os.environ, {
                    'IS_DEBUG_MODE': value, 'PATH_TO_TELEGRAM_SESSION': 'unused',
                    'TELEGRAM_APP_API_ID': '1', 'TELEGRAM_APP_API_HASH': 'unused',
                    'DATABASE_URL': 'unused', 'CACHE_PATH': '',
                }), patch('dotenv.load_dotenv'), patch('telethon.TelegramClient'), patch('sqlalchemy.create_engine'):
                    spec.loader.exec_module(module)
                self.assertIs(module.IS_DEBUG_MODE, expected)


class BotCommandTests(MainTestCase):
    def setUp(self):
        super().setUp()
        self.message.from_user.id = 12345
        self.message.chat.id = 12345
        self.message.chat.type = 'private'
        self.state = Mock(clear=AsyncMock(), set_state=AsyncMock())
        self.store = Mock(save=AsyncMock())
        self.main.monitor = VacancyMonitor(self.store, MonitoringState(), 30,
                                          AsyncMock(return_value=SearchResult()), self.main.error_reporter)

    async def test_private_chat_commands_and_repeated_start(self):
        await self.main.start(self.message, self.state)
        self.assertTrue(self.main.monitor.state.enabled)
        self.assertEqual(self.main.monitor.state.chat_id, 12345)
        due = self.main.monitor.state.next_check_at
        await self.main.start(self.message, self.state)
        self.assertEqual(self.main.monitor.state.next_check_at, due)
        self.assertIn('already enabled', self.message.answer.call_args.args[0])
        await self.main.status(self.message, self.state)
        self.assertIn('Europe/Minsk', self.message.answer.call_args.args[0])
        await self.main.stop(self.message, self.state)
        self.assertFalse(self.main.monitor.state.enabled)

    async def test_group_messages_are_ignored(self):
        self.message.chat.id = -100123
        for kind in ('group', 'supergroup', 'channel'):
            with self.subTest(kind=kind):
                self.message.chat.type = kind
                for handler in (self.main.start, self.main.stop, self.main.status,
                                self.main.set_sleep_time, self.main.save_sleep_time, self.main.cancel):
                    await handler(self.message, self.state)
                self.message.answer.assert_not_awaited()
                self.store.save.assert_not_awaited()
                self.state.clear.assert_not_awaited()

    async def test_another_private_chat_can_control_monitor_and_select_delivery(self):
        await self.main.start(self.message, self.state)
        self.assertEqual(self.main.monitor.state.chat_id, 12345)
        self.message.from_user.id = self.message.chat.id = 999
        await self.main.start(self.message, self.state)
        self.assertEqual(self.main.monitor.state.chat_id, 999)
        self.assertTrue(self.main.monitor.state.enabled)
        await self.main.status(self.message, self.state)
        self.assertIn('Europe/Minsk', self.message.answer.call_args.args[0])
        await self.main.stop(self.message, self.state)
        self.assertFalse(self.main.monitor.state.enabled)

    async def test_sleep_setup_validation_retry_and_off(self):
        await self.main.set_sleep_time(self.message, self.state)
        self.state.set_state.assert_awaited_once_with(self.main.SleepSettings.waiting_for_period)
        self.assertIn('23:00-08:00', self.message.answer.call_args.args[0])
        for value in ('23 00-08 00', '24:00-08:00', '08:00-08:00'):
            self.message.text = value
            await self.main.save_sleep_time(self.message, self.state)
            self.assertIsNone(self.main.monitor.state.sleep)
            self.state.clear.assert_not_awaited()
        self.message.text = '23:00-08:00'
        await self.main.save_sleep_time(self.message, self.state)
        self.assertEqual(str(self.main.monitor.state.sleep), '23:00-08:00')
        self.state.clear.assert_awaited_once()
        self.message.text = ' OFF '
        await self.main.save_sleep_time(self.message, self.state)
        self.assertIsNone(self.main.monitor.state.sleep)
        self.assertFalse(self.main.monitor.state.enabled)

    async def test_cancel_preserves_sleep_period(self):
        await self.main.monitor.set_sleep(SleepPeriod.parse('23:00-08:00'))
        await self.main.cancel(self.message, self.state)
        self.state.clear.assert_awaited_once()
        self.assertEqual(str(self.main.monitor.state.sleep), '23:00-08:00')

    async def test_failed_save_keeps_prompt_and_previous_sleep_period(self):
        await self.main.monitor.set_sleep(SleepPeriod.parse('23:00-08:00'))
        self.store.save.side_effect = RuntimeError('database unavailable')
        self.message.text = '00:00-09:00'
        with self.assertRaises(RuntimeError):
            await self.main.save_sleep_time(self.message, self.state)
        self.state.clear.assert_not_awaited()
        self.assertEqual(str(self.main.monitor.state.sleep), '23:00-08:00')
        self.report.assert_called_once()

    async def test_dispatcher_routes_prompt_replies_and_commands(self):
        from aiogram.types import Chat, Message, Update, User
        bot = Bot('12345:unused')
        self.addAsyncCleanup(bot.session.close)
        reply = self.patch_object(bot, 'session', new=AsyncMock())
        # Exercise real aiogram filters and FSM, with all Telegram I/O mocked.
        async def send(text, update_id):
            message = Message(message_id=update_id, date=CUTOFF, text=text,
                              chat=Chat(id=12345, type='private'),
                              from_user=User(id=12345, is_bot=False, first_name='Owner'))
            return await self.main.dp.feed_update(bot, Update(update_id=update_id, message=message))
        await send('/setsleeptime', 1)
        await send('invalid', 2)
        self.assertIsNone(self.main.monitor.state.sleep)
        await send('23:00-08:00', 3)
        self.assertEqual(str(self.main.monitor.state.sleep), '23:00-08:00')
        await send('/setsleeptime', 4)
        await send('/status', 5)
        await send('00:00-09:00', 6)
        self.assertEqual(str(self.main.monitor.state.sleep), '23:00-08:00')
        await send('/start', 7)
        self.assertTrue(self.main.monitor.state.enabled)
        await send('/stop', 8)
        self.assertFalse(self.main.monitor.state.enabled)
        self.assertGreaterEqual(reply.call_count, 7)

    async def test_startup_restores_monitor_registers_commands_and_drains_worker(self):
        self.config.FETCH_PERIOD_IN_MINUTES = 30
        saved = MonitoringState(enabled=True, chat_id=12345, sleep=SleepPeriod.parse('23:00-08:00'))
        store = Mock(spec=MysqlMonitoringStore, load=AsyncMock(return_value=saved))
        self.patch_object(self.main, 'MysqlMonitoringStore', return_value=store)
        monitor = Mock(run=AsyncMock(), state=saved)
        factory = self.patch_object(self.main, 'VacancyMonitor', return_value=monitor)
        self.patch_object(self.main.Base.metadata, 'create_all')
        self.patch_object(self.main.engine, 'dispose')
        self.patch_object(self.main.client, 'disconnect', new=AsyncMock())
        bot = Mock(set_my_commands=AsyncMock(), send_message=AsyncMock())
        context = MagicMock()
        context.__aenter__.return_value = bot
        self.patch_object(self.main, 'Bot', return_value=context)
        self.patch_object(self.main.dp, 'start_polling', new=AsyncMock())
        with patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': 'unused'}):
            os.environ.pop('TELEGRAM_OWNER_ID', None)
            await self.main.main()
        self.assertIs(factory.call_args.args[1], saved)
        commands = [item.command for item in bot.set_my_commands.call_args.args[0]]
        self.assertEqual(commands, ['start', 'stop', 'status', 'setsleeptime', 'cancel'])
        monitor.run.assert_awaited_once()
        monitor.close.assert_called_once()
        self.main.dp.start_polling.assert_awaited_once_with(bot, close_bot_session=False)
        self.main.client.disconnect.assert_awaited_once()
        async def send_job(send, should_continue, on_source):
            monitor.state = MonitoringState(enabled=True, chat_id=999)
            await send('A vacancy')
        self.patch_object(self.main, 'search_jobs', new=AsyncMock(side_effect=send_job))
        await factory.call_args.args[3](lambda: True, lambda name: None)
        bot.send_message.assert_awaited_once_with(12345, 'A vacancy')
