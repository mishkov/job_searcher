import importlib.util
import os
import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from src.job import Job
from src.job_sources.job_source import JobsSourceConfig
from src.job_sources.remote_ok.remote_ok import RemoteOkFetchError, RemoteOkJobsSourceConfig
from src.search_filter import SearchFilter


CUTOFF = datetime(2026, 10, 12, tzinfo=timezone.utc)


class MainReportingTests(unittest.IsolatedAsyncioTestCase):
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
        self.patch_object(self.main, 'Session', return_value=MagicMock())
        self.repository = Mock(get_last_search_time=AsyncMock(return_value=CUTOFF),
                               save_last_search_time=AsyncMock(), seen=AsyncMock(return_value=False),
                               add_seen_job=AsyncMock())
        self.patch_object(self.main, 'MysqlJobsSearchHistoryRespository', return_value=self.repository)
        self.source = Mock(config=self.config.SOURCES[0], fetch=AsyncMock(return_value=[]))
        self.factory = self.patch_object(self.main, 'RemoteOkJobsSource', return_value=self.source)
        self.status = Mock(edit_text=AsyncMock())
        self.message = Mock(answer=AsyncMock(return_value=self.status))

    def patch_object(self, *args, **kwargs):
        patcher = patch.object(*args, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    async def test_known_source_failure_is_reported_once_and_next_source_runs(self):
        self.config.SOURCES.append(RemoteOkJobsSourceConfig(SearchFilter(keywords=['python'])))
        next_source = Mock(config=self.config.SOURCES[1], fetch=AsyncMock(return_value=[]))
        self.factory.side_effect = [self.source, next_source]
        error = RemoteOkFetchError('request failed')
        self.source.fetch.side_effect = error
        await self.main.jobs(self.message)
        self.report.assert_called_once()
        self.assertIs(self.report.call_args.args[0], error)
        self.assertEqual(self.report.call_args.kwargs['context']['filters']['keywords'], ['flutter'])
        next_source.fetch.assert_awaited_once()
        self.assertTrue(any('Failed: request failed' in call.args[0]
                            for call in self.status.edit_text.call_args_list))

    async def test_unexpected_source_failure_propagates_with_one_report(self):
        error = ValueError('unexpected data')
        self.source.fetch.side_effect = error
        with self.assertRaises(ValueError) as raised:
            await self.main.jobs(self.message)
        self.assertIs(raised.exception, error)
        self.report.assert_called_once()
        self.assertEqual(self.report.call_args.kwargs['context']['operation'], 'fetch_jobs')

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
                    self.message.answer.side_effect = [self.status, error]
                else:
                    self.repository.add_seen_job.side_effect = error
                with self.assertRaises(RuntimeError):
                    await self.main.jobs(self.message)
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
            await self.main.jobs(self.message)
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
            await self.main.jobs(self.message)
        context = self.report.call_args.kwargs['context']
        self.assertEqual(context['source'], 'unknown')
        self.assertEqual(context['operation'], 'create_source')
