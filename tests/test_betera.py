import hashlib
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import requests

from src.error_reporting import ErrorReporter
from src.job_sources.betera.betera import BeteraFetchError, BeteraJobsSource, BeteraJobsSourceConfig
from src.job_sources.error_reporting_source import ErrorReportingJobsSource
from src.search_filter import SearchFilter


CUTOFF = datetime(2099, 1, 1, tzinfo=timezone.utc)
BASE = BeteraJobsSource.BASE_URL
API = BeteraJobsSource.API_URL


def item(identity=29479, slug='data-engineer-2', **kwargs):
    return dict(id=identity, slug=slug, position='Engineer', money=None,
                city='Минск', division='IT', archived_at=None, **kwargs)


def listing(items=None, page=1, total=1):
    return dict(items=[item()] if items is None else items, page=page, total=total)


def detail(**kwargs):
    return {**item(), 'intro': '<p>Build apps.</p>', 'body': '<ul><li>Use FLUTTER &amp; Dart.</li></ul>',
            'requirements': '<p>Experience</p>', 'conditions': '<p>гибридный формат работы</p>',
            'is_archived': False, **kwargs}


def response(data=None, status=200):
    result = Mock(status_code=status)
    result.json.return_value = data
    if status >= 400:
        error_response = requests.Response()
        error_response.status_code = status
        result.raise_for_status.side_effect = requests.HTTPError(response=error_response)
    return result


class BeteraTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patcher = patch('src.job_sources.betera.betera.requests.get')
        self.request = patcher.start()
        self.addCleanup(patcher.stop)
        self.source = self.source_with(SearchFilter(['flutter'], ['unpaid']))
        self.request.side_effect = [response(listing()), response(detail())]

    @staticmethod
    def source_with(filters):
        return BeteraJobsSource(BeteraJobsSourceConfig(filters))

    async def test_full_description_metadata_and_stable_identity(self):
        job, = await self.source.fetch(CUTOFF)
        self.assertEqual(job.title, 'Engineer')
        for text in ('Use FLUTTER & Dart.', 'Experience', 'гибридный формат работы',
                     'Company: Betera', 'Location: Минск', 'Department: IT'):
            self.assertIn(text, job.description)
        self.assertNotIn('<li>', job.description)
        self.assertEqual(job.salary, '-')
        self.assertIs(job.is_remote_allowed, True)
        self.assertEqual(job.url, f'{BASE}/vacancy/data-engineer-2')
        self.assertEqual(job.fingerprint, hashlib.sha256(b'betera.huntflow.io:29479').hexdigest())
        self.request.assert_any_call(API, params={'count': 20, 'page': 1},
                                     headers={'User-Agent': 'Mozilla/5.0'}, timeout=30)
        self.request.side_effect = [response(listing([item(slug='renamed')])),
                                    response(detail(slug='renamed', position='New title', money='5000 BYN'))]
        renamed, = await self.source.fetch(CUTOFF.replace(tzinfo=None))
        self.assertEqual(renamed.fingerprint, job.fingerprint)
        self.assertEqual(renamed.salary, '5000 BYN')

    async def test_full_text_filters_wildcard_and_exclusion_priority(self):
        for filters, expected in ((SearchFilter(['*']), 1), (SearchFilter(['python', 'DART']), 1),
                                  (SearchFilter(['java']), 0), (SearchFilter(['*'], ['EXPERIENCE']), 0)):
            with self.subTest(filters=filters):
                self.request.side_effect = [response(listing()), response(detail())]
                self.assertEqual(len(await self.source_with(filters).fetch(CUTOFF)), expected)
        self.request.reset_mock()
        self.assertEqual(await self.source_with(SearchFilter([])).fetch(CUTOFF), [])
        self.request.assert_not_called()

    async def test_all_pages_deduplicated_and_general_application_excluded(self):
        self.request.side_effect = [
            response(listing([item(), item(), item(27269, 'khochu-rabotat-v-betera')], total=2)),
            response(detail()),
            response(listing([item(), item(2, 'developer')], page=2, total=2)),
            response(detail(id=2, slug='developer')),
        ]
        jobs = await self.source.fetch(CUTOFF)
        self.assertEqual(len(jobs), 2)
        self.assertEqual(self.request.call_count, 4)
        self.request.assert_any_call(API, params={'count': 20, 'page': 2},
                                     headers={'User-Agent': 'Mozilla/5.0'}, timeout=30)

    async def test_empty_listing_and_fresh_next_run(self):
        self.request.side_effect = [response(listing([], total=0)), response(listing()), response(detail())]
        self.assertEqual(await self.source.fetch(CUTOFF), [])
        self.assertEqual(len(await self.source.fetch(CUTOFF)), 1)

    async def test_archived_and_removed_jobs_are_skipped(self):
        for record in (detail(is_archived=True), detail(archived_at='2026-10-01')):
            self.request.side_effect = [response(listing()), response(record)]
            self.assertEqual(await self.source.fetch(CUTOFF), [])
        self.request.side_effect = [response(listing([{**item(), 'archived_at': '2026-10-01'}]))]
        self.assertEqual(await self.source.fetch(CUTOFF), [])
        for status in (404, 410):
            self.request.side_effect = [response(listing()), response(status=status)]
            self.assertEqual(await self.source.fetch(CUTOFF), [])

    async def test_optional_metadata_and_no_remote_inference_from_duties(self):
        self.request.side_effect = [response(listing()), response(detail(
            city=None, division=None, conditions=None, body='<p>Flutter remote work support</p>'))]
        job, = await self.source.fetch(CUTOFF)
        self.assertIsNone(job.is_remote_allowed)
        self.assertNotIn('Location:', job.description)

    async def test_invalid_listing_and_postings_fail(self):
        for data in (None, {}, listing(page=2), listing(total='2'), listing([], total=2),
                     listing([None]), listing([{**item(), 'slug': '../bad'}])):
            with self.subTest(data=data):
                self.request.side_effect = [response(data)]
                with self.assertRaisesRegex(BeteraFetchError, 'invalid job data'):
                    await self.source.fetch(CUTOFF)
        for data in (None, {}, detail(id=1), detail(position=''), detail(body=[]), detail(money=123),
                     detail(intro=None, body=None, requirements=None, conditions=None)):
            with self.subTest(data=data):
                self.request.side_effect = [response(listing()), response(data)]
                with self.assertRaisesRegex(BeteraFetchError, 'invalid job data'):
                    await self.source.fetch(CUTOFF)

    async def test_repeated_pages_fail(self):
        self.request.side_effect = [response(listing(total=3)), response(detail()),
                                    response(listing(page=2, total=3))]
        with self.assertRaisesRegex(BeteraFetchError, 'invalid job data'):
            await self.source.fetch(CUTOFF)

    async def test_http_network_and_json_errors(self):
        for status in (403, 404, 429, 500):
            for on_detail in (False, True):
                if on_detail and status == 404:
                    continue
                self.request.side_effect = ([response(listing())] if on_detail else []) + [response(status=status)]
                with self.assertRaisesRegex(BeteraFetchError, f'HTTP {status}') as raised:
                    await self.source.fetch(CUTOFF)
                if status == 429:
                    self.assertIn('rate limit exceeded', str(raised.exception))
        self.request.side_effect = requests.Timeout()
        with self.assertRaisesRegex(BeteraFetchError, 'network connectivity'):
            await self.source.fetch(CUTOFF)
        invalid = response()
        invalid.json.side_effect = requests.exceptions.JSONDecodeError('bad', 'x', 0)
        self.request.side_effect = [invalid]
        with self.assertRaisesRegex(BeteraFetchError, 'invalid job data'):
            await self.source.fetch(CUTOFF)

    async def test_failed_later_page_has_clean_diagnostic_context(self):
        reporter = Mock(spec=ErrorReporter)
        self.request.side_effect = [response(listing(total=2)), response(detail()), requests.Timeout()]
        with self.assertRaises(BeteraFetchError):
            await ErrorReportingJobsSource(self.source, reporter).fetch(CUTOFF)
        context = reporter.report.call_args.kwargs['context']
        self.assertEqual(context['source'], 'Betera')
        self.assertEqual(context['operation'], 'fetch_listing')
        self.assertEqual(context['page'], 2)
        self.assertIsNone(context['posting'])
