import hashlib
import unittest
from datetime import datetime, timedelta, timezone
from html import escape
from unittest.mock import Mock, patch

import requests

from src.error_reporting import ErrorReporter
from src.job_sources.error_reporting_source import ErrorReportingJobsSource
from src.job_sources.greenhouse.greenhouse import GreenhouseFetchError, GreenhouseJobsSource, GreenhouseJobsSourceConfig
from src.search_filter import SearchFilter


CUTOFF = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
BASE_URL = 'https://boards-api.greenhouse.io/v1/boards/capitalontap/jobs'
JOB_URL = 'https://job-boards.greenhouse.io/capitalontap/jobs/8611607002'


def posting(**changes):
    data = {
        'id': 8611607002, 'title': 'Mobile Engineer', 'company_name': 'Capital on Tap',
        'first_published': CUTOFF.isoformat(), 'updated_at': (CUTOFF + timedelta(days=1)).isoformat(),
        'content': escape('<p>Build FLUTTER &amp; Dart apps.</p><p>Salary: £80,000</p>'),
        'absolute_url': JOB_URL, 'location': {'name': 'London'},
        'departments': [{'id': 4017680002, 'name': 'IT'}], 'metadata': None,
    }
    data.update(changes)
    return data


def listing(*records):
    return {'jobs': list(records), 'meta': {'total': len(records)}}


def response(data, status=200):
    result = Mock(status_code=status)
    result.json.return_value = data
    if status >= 400:
        error_response = requests.Response()
        error_response.status_code = status
        result.raise_for_status.side_effect = requests.HTTPError(response=error_response)
    return result


class GreenhouseConfigTests(unittest.TestCase):
    def test_company_and_filters_have_independent_history(self):
        configs = [GreenhouseJobsSourceConfig(slug, SearchFilter([keyword])) for slug, keyword in
                   [('capitalontap', 'flutter'), ('another-company', 'flutter'), ('capitalontap', 'python')]]
        self.assertEqual(len({config.history_key for config in configs}), 3)
        self.assertEqual(configs[0].history_key,
                         GreenhouseJobsSourceConfig('capitalontap', SearchFilter(['FLUTTER'])).history_key)
        self.assertEqual(configs[0].name, 'Greenhouse: capitalontap')

    def test_invalid_slugs(self):
        for slug in ('', ' ', '../capitalontap', 'capitalontap/jobs', 'capitalontap?x=1',
                     'https://job-boards.greenhouse.io/capitalontap', 'capitalontap.', None):
            with self.subTest(slug=slug), self.assertRaisesRegex(ValueError, 'company_slug'):
                GreenhouseJobsSourceConfig(slug, SearchFilter())


class GreenhouseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.source = self.source_with(SearchFilter(['flutter']))
        patcher = patch('src.job_sources.greenhouse.greenhouse.requests.get')
        self.request = patcher.start()
        self.addCleanup(patcher.stop)
        self.request.return_value = response(listing(posting()))

    @staticmethod
    def source_with(filters, slug='capitalontap'):
        return GreenhouseJobsSource(GreenhouseJobsSourceConfig(slug, filters))

    async def test_full_description_metadata_and_request(self):
        job, = await self.source.fetch(CUTOFF)
        self.assertEqual(job.title, 'Mobile Engineer')
        for text in ('Build FLUTTER & Dart apps.', 'Salary: £80,000', 'Company: Capital on Tap',
                     'Location: London', 'Department: IT'):
            self.assertIn(text, job.description)
        self.assertNotIn('<p>', job.description)
        self.assertNotIn('&amp;', job.description)
        self.assertEqual(job.salary, '-')
        self.assertIsNone(job.is_remote_allowed)
        self.assertEqual(job.url, JOB_URL)
        self.request.assert_called_once_with(BASE_URL, params={'content': 'true'}, timeout=30)

    async def test_filters_and_exclusion_priority(self):
        for filters, count in [(SearchFilter(['DART']), 1), (SearchFilter(['python']), 0),
                               (SearchFilter(['*']), 1), (SearchFilter(['*'], ['flutter']), 0),
                               (SearchFilter(['flutter'], ['£80,000']), 0)]:
            with self.subTest(filters=filters):
                self.assertEqual(len(await self.source_with(filters).fetch(CUTOFF)), count)
        self.request.reset_mock()
        self.assertEqual(await self.source_with(SearchFilter([])).fetch(CUTOFF), [])
        self.request.assert_not_called()

    async def test_publication_date_boundary_and_update_fallback(self):
        for published, since, count in [(CUTOFF.isoformat(), CUTOFF, 1),
                                       ('2026-10-26T08:00:00-04:00', CUTOFF, 1),
                                       ('2026-10-26T12:00:00', CUTOFF.replace(tzinfo=None), 1),
                                       ((CUTOFF - timedelta(seconds=1)).isoformat(), CUTOFF, 0)]:
            with self.subTest(published=published):
                self.request.return_value = response(listing(posting(first_published=published)))
                self.assertEqual(len(await self.source.fetch(since)), count)
        for published in (None, ''):
            self.request.return_value = response(listing(posting(first_published=published, updated_at=CUTOFF.isoformat())))
            self.assertEqual(len(await self.source.fetch(CUTOFF)), 1)
        data = posting()
        del data['first_published']
        self.request.return_value = response(listing(data))
        self.assertEqual(len(await self.source.fetch(CUTOFF)), 1)

    async def test_unsorted_jobs_duplicates_and_fresh_listing(self):
        self.request.side_effect = [response(listing(
            posting(first_published='2020-01-01T00:00:00Z'), posting(id=2), posting(id=2))), response(listing())]
        jobs = await self.source.fetch(CUTOFF)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(await self.source.fetch(CUTOFF), [])
        self.assertEqual(self.request.call_count, 2)

    async def test_stable_fingerprints_and_separate_company_endpoints(self):
        job, = await self.source.fetch(CUTOFF)
        self.assertEqual(job.fingerprint, hashlib.sha256(b'greenhouse.io:capitalontap:8611607002').hexdigest())
        self.request.return_value = response(listing(posting(title='Renamed', absolute_url=JOB_URL + '?tracking=1')))
        renamed, = await self.source.fetch(CUTOFF)
        self.assertEqual(job.fingerprint, renamed.fingerprint)
        other, = await self.source_with(SearchFilter(), 'another-company').fetch(CUTOFF)
        self.assertNotEqual(job.fingerprint, other.fingerprint)
        self.assertEqual(self.request.call_args.args, ('https://boards-api.greenhouse.io/v1/boards/another-company/jobs',))

    async def test_custom_urls_and_optional_metadata(self):
        self.request.return_value = response(listing(posting(
            absolute_url='https://careers.example.com/?gh_jid=8611607002#apply', company_name=None,
            departments=[], location={'name': 'Remote'}, content=escape(escape('<p>Flutter &amp; Dart</p>')))))
        job, = await self.source.fetch(CUTOFF)
        self.assertEqual(job.url, 'https://careers.example.com/?gh_jid=8611607002')
        self.assertIn('Company: capitalontap', job.description)
        self.assertIn('Flutter & Dart', job.description)
        self.assertIs(job.is_remote_allowed, True)

    async def test_invalid_data_fails_instead_of_advancing_history(self):
        for data in (None, [], {}, {'jobs': None}, {'jobs': []}, {'jobs': [], 'meta': {'total': 1}},
                     {'jobs': [], 'meta': {'total': False}}, listing(None), listing('bad'),
                     listing(posting(id=True)), listing(posting(id=-1)), listing(posting(id='123')),
                     listing(posting(first_published='bad')), listing(posting(title='')),
                     listing(posting(content='<p></p>')), listing(posting(content=[])),
                     listing(posting(location=None)), listing(posting(departments='bad')),
                     listing(posting(absolute_url='javascript:alert(1)')),
                     listing(posting(absolute_url='https://user:password@example.com/job'))):
            with self.subTest(data=data):
                self.request.return_value = response(data)
                with self.assertRaisesRegex(GreenhouseFetchError, 'invalid job data'):
                    await self.source.fetch(CUTOFF)
        self.request.return_value = response(None)
        self.request.return_value.json.side_effect = ValueError('bad JSON')
        with self.assertRaisesRegex(GreenhouseFetchError, 'invalid job data'):
            await self.source.fetch(CUTOFF)

    async def test_http_and_network_failures(self):
        for status in (403, 404, 429, 500):
            with self.subTest(status=status):
                self.request.return_value = response(None, status)
                with self.assertRaisesRegex(GreenhouseFetchError, f'HTTP {status}') as raised:
                    await self.source.fetch(CUTOFF)
                if status == 429:
                    self.assertIn('rate limit exceeded', str(raised.exception))
        self.request.side_effect = requests.Timeout()
        with self.assertRaisesRegex(GreenhouseFetchError, 'network connectivity'):
            await self.source.fetch(CUTOFF)

    async def test_failure_context_includes_company_and_posting(self):
        self.request.return_value = response(listing(posting(content='')))
        reporter = Mock(spec=ErrorReporter)
        with self.assertRaises(GreenhouseFetchError):
            await ErrorReportingJobsSource(self.source, reporter).fetch(CUTOFF)
        reporter.report.assert_called_once()
        context = reporter.report.call_args.kwargs['context']
        self.assertEqual(context['source'], 'Greenhouse: capitalontap')
        self.assertEqual(context['operation'], 'parse_posting')
        self.assertEqual(context['posting']['id'], 8611607002)
