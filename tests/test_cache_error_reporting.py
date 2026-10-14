import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from src.error_reporting import ErrorReporter, report_errors
from src.job_sources.nodesk.api import NoDeskApi
from src.job_sources.nodesk.sql_cached_api import SqlCachedNoDeskApi
from src.job_sources.relocate_me.api import RelocateMeApi
from src.job_sources.relocate_me.sql_cached_api import SqlCachedRelocateMeApi
from src.job_sources.we_work_remotely.api import CATEGORIES, FIELDS, FeedResponse, WeWorkRemotelyApi
from src.job_sources.we_work_remotely.sql_cached_api import SqlCachedWeWorkRemotelyApi


class CacheReportingTests(unittest.TestCase):
    def cases(self, directory, reporter):
        nodesk = Mock(spec=NoDeskApi)
        nodesk.get_job_posting.return_value = {'title': 'Engineer', 'description': 'Flutter'}
        relocate = Mock(spec=RelocateMeApi)
        relocate.get_job_posting.return_value = {'id': '123', 'title': 'Engineer', 'description': 'Flutter',
                                                'salary': '-', 'date_posted': '2026-10-12', 'remote': None}
        wwr = Mock(spec=WeWorkRemotelyApi)
        job = {**dict.fromkeys(FIELDS, ''), 'title': 'Engineer', 'description': 'Flutter',
               'link': 'https://weworkremotely.com/remote-jobs/engineer', 'pubDate': 'Mon, 12 Oct 2026 10:00:00 GMT'}
        wwr.get_feed.return_value = FeedResponse([job], etag='one')
        return [
            ('NoDesk', SqlCachedNoDeskApi(nodesk, directory, reporter=reporter),
             'get_job_posting', 'https://nodesk.co/remote-jobs/engineer/', nodesk.get_job_posting.return_value),
            ('Relocate.me', SqlCachedRelocateMeApi(relocate, directory, reporter=reporter),
             'get_job_posting', 'https://relocate.me/japan/tokyo/example/engineer-123', relocate.get_job_posting.return_value),
            ('We Work Remotely', SqlCachedWeWorkRemotelyApi(wwr, directory, reporter=reporter),
             'get_feed', CATEGORIES[0], wwr.get_feed.return_value),
        ]

    def test_corrupt_cache_reports_read_and_write_failures_but_returns_upstream_data(self):
        with tempfile.TemporaryDirectory() as directory:
            reporter = Mock(spec=ErrorReporter)
            for source, cache, method, key, expected in self.cases(directory, reporter):
                with self.subTest(source=source):
                    reporter.reset_mock()
                    cache._database_path.parent.mkdir(parents=True)
                    cache._database_path.write_bytes(b'corrupt database')
                    with report_errors(reporter, filters={'keywords': ['flutter']}, since='2026-10-12'):
                        self.assertEqual(getattr(cache, method)(key), expected)
                    self.assertEqual(reporter.report.call_count, 2)
                    contexts = [call.kwargs['context'] for call in reporter.report.call_args_list]
                    self.assertEqual([context['operation'] for context in contexts], ['cache_read', 'cache_write'])
                    for context in contexts:
                        self.assertEqual(context['source'], source)
                        self.assertEqual(context['filters'], {'keywords': ['flutter']})
                        self.assertEqual(context['cache_path'], str(cache._database_path))
                        self.assertEqual(context['since'], '2026-10-12')
                        self.assertEqual(context.get('category', context.get('posting', {}).get('url')), key)

    def test_missing_cache_and_successful_writes_are_not_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            reporter = Mock(spec=ErrorReporter)
            for source, cache, method, key, expected in self.cases(directory, reporter):
                with self.subTest(source=source):
                    self.assertEqual(getattr(cache, method)(key), expected)
            reporter.report.assert_not_called()

    def test_backend_failure_does_not_break_cache_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            reporter = Mock(spec=ErrorReporter)
            reporter.report.side_effect = RuntimeError('reporter unavailable')
            path = Path(directory) / 'not-a-directory'
            path.write_text('keep me')
            for source, cache, method, key, expected in self.cases(path, reporter):
                with self.subTest(source=source), self.assertLogs('src.error_reporting', level='ERROR'):
                    self.assertEqual(getattr(cache, method)(key), expected)
            self.assertEqual(path.read_text(), 'keep me')
