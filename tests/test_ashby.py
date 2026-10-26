import hashlib
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import requests

from src.error_reporting import ErrorReporter
from src.job_sources.ashby.ashby import AshbyFetchError, AshbyJobsSource, AshbyJobsSourceConfig
from src.job_sources.error_reporting_source import ErrorReportingJobsSource
from src.search_filter import SearchFilter


CUTOFF = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
JOB_ID = 'bbbf32b0-5de0-4280-b40f-bcc30378dd7b'
JOB_URL = f'https://jobs.ashbyhq.com/gismart/{JOB_ID}'
BASE_URL = 'https://api.ashbyhq.com/posting-api/job-board/gismart'


def posting(**changes):
    data = {
        'id': JOB_ID, 'title': 'Mobile Developer (Testora)', 'department': 'Engineering',
        'team': 'Testora', 'employmentType': 'FullTime', 'location': 'Kyiv',
        'secondaryLocations': [{'location': 'Remote (Ukraine)', 'address': {'postalAddress': {}}}],
        'publishedAt': CUTOFF.isoformat(), 'isListed': True, 'isRemote': True,
        'workplaceType': 'Hybrid', 'jobUrl': JOB_URL, 'applyUrl': JOB_URL + '/application',
        'descriptionPlain': 'Build FLUTTER & Dart apps.\nWork with our product team.',
        'descriptionHtml': '<p>Build FLUTTER &amp; Dart apps.</p><p>Work with our product team.</p>',
        'compensation': {'scrapeableCompensationSalarySummary': None, 'compensationTierSummary': None},
    }
    data.update(changes)
    return data


def listing(*records):
    return {'apiVersion': '1', 'jobs': list(records)}


def response(data, status=200):
    result = Mock(status_code=status)
    result.json.return_value = data
    if status >= 400:
        error_response = requests.Response()
        error_response.status_code = status
        result.raise_for_status.side_effect = requests.HTTPError(response=error_response)
    return result


class AshbyConfigTests(unittest.TestCase):
    def test_company_and_filters_have_independent_history(self):
        configs = [AshbyJobsSourceConfig(slug, SearchFilter([keyword])) for slug, keyword in
                   [('gismart', 'flutter'), ('another-company', 'flutter'), ('gismart', 'python')]]
        self.assertEqual(len({config.history_key for config in configs}), 3)
        self.assertEqual(configs[0].history_key,
                         AshbyJobsSourceConfig('gismart', SearchFilter(['FLUTTER'])).history_key)
        self.assertEqual(configs[0].name, 'Ashby: gismart')

    def test_invalid_slugs(self):
        for slug in ('', ' ', '../gismart', 'gismart/jobs', 'gismart?x=1',
                     'https://jobs.ashbyhq.com/gismart', 'gismart.', None):
            with self.subTest(slug=slug), self.assertRaisesRegex(ValueError, 'company_slug'):
                AshbyJobsSourceConfig(slug, SearchFilter())


class AshbyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.source = self.source_with(SearchFilter(['flutter']))
        patcher = patch('src.job_sources.ashby.ashby.requests.get')
        self.request = patcher.start()
        self.addCleanup(patcher.stop)
        self.request.return_value = response(listing(posting()))

    @staticmethod
    def source_with(filters, slug='gismart'):
        return AshbyJobsSource(AshbyJobsSourceConfig(slug, filters))

    async def test_full_description_metadata_and_request(self):
        job, = await self.source.fetch(CUTOFF)
        self.assertEqual(job.title, 'Mobile Developer (Testora)')
        for text in ('Build FLUTTER & Dart apps.', 'Company: gismart', 'Location: Kyiv, Remote (Ukraine)',
                     'Department: Engineering', 'Team: Testora', 'Employment: FullTime', 'Workplace: Hybrid'):
            self.assertIn(text, job.description)
        self.assertNotIn('<p>', job.description)
        self.assertEqual(job.salary, '-')
        self.assertIs(job.is_remote_allowed, True)
        self.assertEqual(job.url, JOB_URL)
        self.request.assert_called_once_with(BASE_URL, params={'includeCompensation': 'true'}, timeout=30)

    async def test_filters_and_exclusion_priority(self):
        for filters, count in [(SearchFilter(['DART']), 1), (SearchFilter(['python']), 0),
                               (SearchFilter(['*']), 1), (SearchFilter(['*'], ['flutter']), 0),
                               (SearchFilter(['flutter'], ['ukraine']), 0)]:
            with self.subTest(filters=filters):
                self.assertEqual(len(await self.source_with(filters).fetch(CUTOFF)), count)
        self.request.reset_mock()
        self.assertEqual(await self.source_with(SearchFilter([])).fetch(CUTOFF), [])
        self.request.assert_not_called()

    async def test_publication_date_boundary_and_timezones(self):
        for published, since, count in [(CUTOFF.isoformat(), CUTOFF, 1),
                                       ('2026-10-26T08:00:00-04:00', CUTOFF, 1),
                                       ('2026-10-26T12:00:00', CUTOFF.replace(tzinfo=None), 1),
                                       ((CUTOFF - timedelta(seconds=1)).isoformat(), CUTOFF, 0)]:
            with self.subTest(published=published):
                self.request.return_value = response(listing(posting(publishedAt=published)))
                self.assertEqual(len(await self.source.fetch(since)), count)

    async def test_unsorted_jobs_duplicates_unlisted_and_fresh_listing(self):
        other_id = 'f263cdd9-c279-4cea-b3c4-d11c90e367b3'
        self.request.side_effect = [response(listing(
            posting(publishedAt='2020-01-01T00:00:00Z'),
            posting(jobUrl=f'https://jobs.ashbyhq.com/gismart/{other_id}'),
            posting(jobUrl=f'https://jobs.ashbyhq.com/gismart/{other_id}?tracking=1'),
            posting(isListed=False))), response(listing())]
        jobs = await self.source.fetch(CUTOFF)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].url, f'https://jobs.ashbyhq.com/gismart/{other_id}')
        self.assertEqual(await self.source.fetch(CUTOFF), [])
        self.assertEqual(self.request.call_count, 2)

    async def test_stable_fingerprint_uses_documented_url_without_optional_id(self):
        job, = await self.source.fetch(CUTOFF)
        self.assertEqual(job.fingerprint, hashlib.sha256(f'ashbyhq.com:gismart:{JOB_ID}'.encode()).hexdigest())
        data = posting(title='Renamed', jobUrl=JOB_URL + '?tracking=1#apply')
        del data['id']
        self.request.return_value = response(listing(data))
        renamed, = await self.source.fetch(CUTOFF)
        self.assertEqual(job.fingerprint, renamed.fingerprint)
        self.assertEqual(renamed.url, JOB_URL)
        data['jobUrl'] = f'https://jobs.ashbyhq.com/another-company/{JOB_ID}'
        self.request.return_value = response(listing(data))
        other, = await self.source_with(SearchFilter(), 'another-company').fetch(CUTOFF)
        self.assertNotEqual(job.fingerprint, other.fingerprint)
        self.assertEqual(self.request.call_args.args, ('https://api.ashbyhq.com/posting-api/job-board/another-company',))

    async def test_html_fallback_and_missing_optional_metadata(self):
        data = {key: value for key, value in posting().items() if key in
                ('title', 'jobUrl', 'publishedAt', 'isListed', 'descriptionHtml')}
        data['descriptionHtml'] += '<script>ignored</script><style>ignored</style>'
        self.request.return_value = response(listing(data))
        job, = await self.source.fetch(CUTOFF)
        self.assertIn('Build FLUTTER & Dart apps.', job.description)
        self.assertNotIn('ignored', job.description)
        self.assertIsNone(job.is_remote_allowed)
        self.assertEqual(job.salary, '-')

    async def test_remote_hybrid_onsite_and_unknown_workplaces(self):
        for workplace, is_remote, expected in [('Remote', True, True), ('Hybrid', False, True),
                                              ('OnSite', True, False), (None, True, True),
                                              (None, False, False), (None, None, None)]:
            with self.subTest(workplace=workplace, is_remote=is_remote):
                self.request.return_value = response(listing(posting(workplaceType=workplace, isRemote=is_remote)))
                job, = await self.source.fetch(CUTOFF)
                self.assertIs(job.is_remote_allowed, expected)

    async def test_compensation_is_displayed_and_filterable(self):
        for summary in (None, '$80K – $100K • Offers Bonus'):
            self.request.return_value = response(listing(posting(compensation={
                'scrapeableCompensationSalarySummary': '$80K - $100K', 'compensationTierSummary': summary})))
            job, = await self.source.fetch(CUTOFF)
            self.assertEqual(job.salary, '$80K - $100K')
            self.assertIn('Compensation: ' + (summary or job.salary), job.description)
            self.assertEqual(await self.source_with(SearchFilter(['flutter'], ['$80K'])).fetch(CUTOFF), [])

    async def test_invalid_data_fails_instead_of_advancing_history(self):
        for data in (None, [], {}, {'jobs': None}, listing(None), listing('bad'),
                     listing(posting(isListed='true')), listing(posting(publishedAt=None)),
                     listing(posting(publishedAt='bad')), listing(posting(title='')),
                     listing(posting(descriptionPlain=None, descriptionHtml='<p></p>')),
                     listing(posting(descriptionPlain=[], descriptionHtml=None)),
                     listing(posting(secondaryLocations='bad')), listing(posting(secondaryLocations=[None])),
                     listing(posting(compensation='bad')), listing(posting(isRemote='true')),
                     listing(posting(jobUrl='javascript:alert(1)')),
                     listing(posting(jobUrl='https://jobs.ashbyhq.com/gismart/bad')),
                     listing(posting(jobUrl=JOB_URL.replace('/gismart/', '/another-company/'))),
                     listing(posting(jobUrl=JOB_URL.replace('jobs.ashbyhq.com', 'user:secret@jobs.ashbyhq.com')))):
            with self.subTest(data=data):
                self.request.return_value = response(data)
                with self.assertRaisesRegex(AshbyFetchError, 'invalid job data'):
                    await self.source.fetch(CUTOFF)
        self.request.return_value = response(None)
        self.request.return_value.json.side_effect = ValueError('bad JSON')
        with self.assertRaisesRegex(AshbyFetchError, 'invalid job data'):
            await self.source.fetch(CUTOFF)

    async def test_http_and_network_failures(self):
        for status in (403, 404, 429, 500):
            with self.subTest(status=status):
                self.request.return_value = response(None, status)
                with self.assertRaisesRegex(AshbyFetchError, f'HTTP {status}') as raised:
                    await self.source.fetch(CUTOFF)
                if status == 429:
                    self.assertIn('rate limit exceeded', str(raised.exception))
        for error in (requests.Timeout(), requests.ConnectionError()):
            self.request.side_effect = error
            with self.assertRaisesRegex(AshbyFetchError, 'network connectivity'):
                await self.source.fetch(CUTOFF)

    async def test_failure_context_includes_company_and_posting(self):
        self.request.return_value = response(listing(posting(title='')))
        reporter = Mock(spec=ErrorReporter)
        with self.assertRaises(AshbyFetchError):
            await ErrorReportingJobsSource(self.source, reporter).fetch(CUTOFF)
        reporter.report.assert_called_once()
        context = reporter.report.call_args.kwargs['context']
        self.assertEqual(context['source'], 'Ashby: gismart')
        self.assertEqual(context['operation'], 'parse_posting')
        self.assertEqual(context['posting']['id'], JOB_ID)
