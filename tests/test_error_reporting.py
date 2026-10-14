import asyncio
import io
import json
import logging
import threading
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock, patch

import requests

from src.error_reporting import (
    ConsoleErrorReporter, ErrorReporter, posting_context, report_error,
    report_errors, update_error_context,
)
from src.job_sources.error_reporting_source import ErrorReportingJobsSource
from src.job_sources.job_source import JobsSourceConfig
from src.job_sources.remote_ok.remote_ok import RemoteOkFetchError, RemoteOkJobsSource, RemoteOkJobsSourceConfig
from src.job_sources.web3_career.web3_career import Web3CareerFetchError, Web3CareerJobsSource, Web3CareerJobsSourceConfig
from src.search_filter import SearchFilter


CUTOFF = datetime(2026, 10, 12, tzinfo=timezone.utc)


class ConsoleReporterTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.logger = logging.Logger('test-reporter')
        self.logger.addHandler(logging.StreamHandler(self.output))
        self.reporter = ConsoleErrorReporter(self.logger)

    def test_context_and_original_traceback_are_printed_outside_except(self):
        try:
            raise ValueError('bad posting')
        except ValueError as caught:
            error = caught
        context = {'source': 'example', 'filters': {'keywords': ['flutter']},
                   'since': CUTOFF, 'posting': {'id': '123', 'title': 'Engineer'}}
        self.reporter.report(error, context=context)
        lines = self.output.getvalue().splitlines()
        self.assertEqual(json.loads(lines[0].removeprefix('Error context: ')),
                         {**context, 'since': str(CUTOFF)})
        self.assertIn('Traceback (most recent call last)', self.output.getvalue())
        self.assertIn("raise ValueError('bad posting')", self.output.getvalue())
        self.assertIn('ValueError: bad posting', self.output.getvalue())

    def test_suppressed_exception_context_is_not_printed(self):
        try:
            try:
                raise requests.ConnectionError('private-token-in-url')
            except requests.RequestException:
                raise RuntimeError('request failed') from None
        except RuntimeError as error:
            self.reporter.report(error, context={})
        self.assertIn('RuntimeError: request failed', self.output.getvalue())
        self.assertNotIn('ConnectionError:', self.output.getvalue())
        self.assertNotIn('private-token-in-url', self.output.getvalue())

    def test_posting_context_selects_identifiers_and_removes_url_credentials(self):
        context = posting_context({'id': 1, 'title': 'Engineer', 'description': 'private copy',
                                   'api_token': 'secret', 'url': 'https://user:secret@example.com/job?token=secret#secret',
                                   'apply_url': 'https://example.com/[invalid', 'position': {'secret': 'value'}})
        self.assertEqual(context, {'id': 1, 'title': 'Engineer', 'url': 'https://example.com/job',
                                   'apply_url': 'https://example.com/[invalid'})
        self.assertEqual(posting_context(None), {})
        self.assertEqual(posting_context({'url': 'https://[invalid'}), {})


class ReportingScopeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.reporter = Mock(spec=ErrorReporter)

    async def test_nested_scopes_report_once_and_preserve_exception_identity(self):
        error = RuntimeError('failure')
        with self.assertRaises(RuntimeError) as raised:
            with report_errors(self.reporter, operation='search', source='example'):
                with report_errors(self.reporter, operation='parse_posting'):
                    update_error_context(posting={'id': '123'})
                    raise error
        self.assertIs(raised.exception, error)
        self.reporter.report.assert_called_once_with(error, context={
            'operation': 'parse_posting', 'source': 'example', 'posting': {'id': '123'},
        })
        report_error(self.reporter, error, operation='outside')
        self.assertEqual(self.reporter.report.call_args.kwargs['context'], {'operation': 'outside'})

    async def test_handled_nested_failure_does_not_pollute_later_reports(self):
        with report_errors(self.reporter, operation='search'):
            try:
                with report_errors(self.reporter, source='failed', posting={'id': 'old'}):
                    raise ValueError('first')
            except ValueError:
                pass
            report_error(self.reporter, RuntimeError('second'), source='next')
        self.assertEqual(self.reporter.report.call_count, 2)
        self.assertEqual(self.reporter.report.call_args.kwargs['context'],
                         {'operation': 'search', 'source': 'next'})

    async def test_separate_attempts_report_even_if_dependency_reuses_exception(self):
        error = RuntimeError('request failed')
        with report_errors(self.reporter, operation='search'):
            for source in ('first', 'second'):
                try:
                    with report_errors(self.reporter, source=source):
                        raise error
                except RuntimeError:
                    pass
        contexts = [call.kwargs['context'] for call in self.reporter.report.call_args_list]
        self.assertEqual(contexts, [{'operation': 'search', 'source': 'first'},
                                    {'operation': 'search', 'source': 'second'}])

    async def test_concurrent_tasks_and_worker_threads_keep_separate_context(self):
        barrier = threading.Barrier(2)

        def fail(identity):
            update_error_context(posting={'id': identity})
            barrier.wait(timeout=5)
            raise ValueError(identity)

        async def search(identity):
            with report_errors(self.reporter, source=identity):
                await asyncio.to_thread(fail, identity)

        results = await asyncio.gather(search('first'), search('second'), return_exceptions=True)
        self.assertTrue(all(isinstance(result, ValueError) for result in results))
        contexts = [call.kwargs['context'] for call in self.reporter.report.call_args_list]
        self.assertCountEqual(contexts, [
            {'source': 'first', 'posting': {'id': 'first'}},
            {'source': 'second', 'posting': {'id': 'second'}},
        ])

    async def test_cancellation_is_not_reported_as_an_error(self):
        with self.assertRaises(asyncio.CancelledError):
            with report_errors(self.reporter):
                raise asyncio.CancelledError()
        self.reporter.report.assert_not_called()

    async def test_backend_failure_does_not_replace_original_failure(self):
        self.reporter.report.side_effect = RuntimeError('backend failed')
        original = ValueError('posting failed')
        with self.assertLogs('src.error_reporting', level='ERROR'):
            with self.assertRaises(ValueError) as raised:
                with report_errors(self.reporter):
                    raise original
        self.assertIs(raised.exception, original)

    async def test_success_returns_the_original_jobs_without_reporting(self):
        jobs = [Mock()]
        source = Mock(config=JobsSourceConfig('example', SearchFilter()), fetch=AsyncMock(return_value=jobs))
        self.assertIs(await ErrorReportingJobsSource(source, self.reporter).fetch(CUTOFF), jobs)
        self.reporter.report.assert_not_called()

    async def test_remote_ok_invalid_record_reports_posting_and_filters(self):
        filters = SearchFilter(keywords=['flutter'], exclude_keywords=['senior'])
        source = RemoteOkJobsSource(RemoteOkJobsSourceConfig(filters))
        record = {'id': 'broken', 'position': 'Flutter Engineer', 'epoch': 'invalid'}
        response = Mock()
        response.json.return_value = [record]
        with patch('src.job_sources.remote_ok.remote_ok.requests.get', return_value=response):
            with self.assertRaises(RemoteOkFetchError) as raised:
                await ErrorReportingJobsSource(source, self.reporter).fetch(CUTOFF)
        self.reporter.report.assert_called_once()
        self.assertIs(self.reporter.report.call_args.args[0], raised.exception)
        self.assertEqual(self.reporter.report.call_args.kwargs['context'], {
            'source': 'Remote OK', 'source_type': 'RemoteOkJobsSourceConfig',
            'filters': {'keywords': ['flutter'], 'exclude_keywords': ['senior']},
            'since': CUTOFF.isoformat(), 'operation': 'parse_posting',
            'posting': {'id': 'broken', 'position': 'Flutter Engineer'},
        })
        filters.keywords.append('python')
        self.assertEqual(self.reporter.report.call_args.kwargs['context']['filters']['keywords'], ['flutter'])

    async def test_web3_request_failure_does_not_include_config_token(self):
        source = Web3CareerJobsSource(Web3CareerJobsSourceConfig(SearchFilter(), api_token='private-token'))
        with patch('src.job_sources.web3_career.web3_career.requests.get',
                   side_effect=requests.Timeout('https://example.com?token=private-token')):
            with self.assertRaises(Web3CareerFetchError):
                await ErrorReportingJobsSource(source, self.reporter).fetch(CUTOFF)
        error = self.reporter.report.call_args.args[0]
        context = self.reporter.report.call_args.kwargs['context']
        self.assertNotIn('private-token', str(error))
        self.assertNotIn('private-token', json.dumps(context))
