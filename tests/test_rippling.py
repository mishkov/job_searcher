import hashlib
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import requests

from src.error_reporting import ErrorReporter
from src.job_sources.error_reporting_source import ErrorReportingJobsSource
from src.job_sources.rippling.rippling import RipplingFetchError, RipplingJobsSource, RipplingJobsSourceConfig
from src.search_filter import SearchFilter


CUTOFF = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)
JOB_ID = 'f042848e-dd13-445d-a062-1e65f52e71c7'
OTHER_ID = '2e270acc-70f9-4a41-82ac-be6732a2f89b'
BOARD_URL = 'https://ats.rippling.com/en-US/sparq/jobs'
JOB_URL = f'https://ats.rippling.com/sparq/jobs/{JOB_ID}'


def board(slug='sparq'):
    return {'slug': slug, 'companyName': 'Example Company'}


def record(**overrides):
    return {
        'id': JOB_ID, 'name': 'Mobile Engineer',
        'locations': [{'name': 'Colombia', 'workplaceType': 'REMOTE'},
                      {'name': 'Costa Rica', 'workplaceType': 'REMOTE'}],
        'department': {'name': 'Engineering'}, **overrides,
    }


def listing(records=None, *, page=0, total=None, size=100, **overrides):
    records = [record()] if records is None else records
    total = len(records) if total is None else total
    data = {'items': records, 'page': page, 'pageSize': size, 'totalItems': total,
            'totalPages': (total + size - 1) // size, **overrides}
    return {'apiData': {'jobBoard': board()}, 'dehydratedState': {'queries': [
        {'queryKey': ['board', 'sparq', 'locations'], 'state': {'data': {'items': []}}},
        {'queryKey': ['board', 'sparq', 'job-posts', True, {'page': page}], 'state': {'data': data}},
    ]}}


def detail(**overrides):
    return {'apiData': {'jobBoard': board(), 'jobPost': {
        'uuid': JOB_ID, 'name': 'Mobile Engineer', 'createdOn': CUTOFF.isoformat(),
        'description': {'role': '<p>Build <b>Flutter</b> apps &amp; services.</p><p>Salary: $100k</p>',
                        'company': '<p>A software company</p>'}, **overrides,
    }}}


def response(props, status=200):
    html = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps({"props": {"pageProps": props}})}</script>'
    return Mock(status_code=status, text=html)


class RipplingConfigTests(unittest.TestCase):
    def test_company_and_filters_have_independent_history(self):
        configs = [RipplingJobsSourceConfig(slug, SearchFilter([keyword]))
                   for slug, keyword in [('sparq', 'flutter'), ('another-company', 'flutter'), ('sparq', 'python')]]
        self.assertEqual(len({config.history_key for config in configs}), 3)
        self.assertEqual(configs[0].history_key,
                         RipplingJobsSourceConfig('sparq', SearchFilter(['FLUTTER'])).history_key)
        self.assertEqual(configs[0].name, 'Rippling: sparq')

    def test_invalid_slugs_fail_before_requests(self):
        for slug in ('', ' ', '../sparq', 'sparq/jobs', 'sparq?x=1', 'https://ats.rippling.com/sparq/jobs', None):
            with self.subTest(slug=slug), self.assertRaisesRegex(ValueError, 'company_slug'):
                RipplingJobsSourceConfig(slug, SearchFilter())


class RipplingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.source = self.source_with(SearchFilter(['flutter']))
        patcher = patch('src.job_sources.rippling.rippling.requests.get')
        self.request = patcher.start()
        self.addCleanup(patcher.stop)
        self.request.side_effect = [response(listing()), response(detail())]

    @staticmethod
    def source_with(filters):
        return RipplingJobsSource(RipplingJobsSourceConfig('sparq', filters))

    async def test_full_description_metadata_and_requests(self):
        job, = await self.source.fetch(CUTOFF)
        self.assertEqual(job.title, 'Mobile Engineer')
        self.assertIn('apps & services.', job.description)
        self.assertIn('Salary: $100k', job.description)
        self.assertIn('Company: Example Company', job.description)
        self.assertIn('Location: Colombia, Costa Rica', job.description)
        self.assertIn('Department: Engineering', job.description)
        self.assertNotIn('<p>', job.description)
        self.assertTrue(job.is_remote_allowed)
        self.assertEqual(job.url, JOB_URL)
        self.assertEqual(job.fingerprint, hashlib.sha256(f'ats.rippling.com:sparq:{JOB_ID}'.encode()).hexdigest())
        self.assertEqual(self.request.call_args_list[0].args, (BOARD_URL,))
        self.assertEqual(self.request.call_args_list[0].kwargs, {'params': {'page': 0, 'pageSize': 100}, 'timeout': 30})
        self.assertEqual(self.request.call_args_list[1].args, (f'{BOARD_URL}/{JOB_ID}',))

    async def test_pagination_deduplication_and_no_early_stop_for_old_jobs(self):
        self.request.side_effect = [
            response(listing([record(), record()], total=3, size=2)),
            response(detail(createdOn='2020-01-01T00:00:00Z')),
            response(listing([record(id=OTHER_ID)], page=1, total=3, size=2)),
            response(detail(uuid=OTHER_ID)),
        ]
        job, = await self.source.fetch(CUTOFF)
        self.assertTrue(job.url.endswith(OTHER_ID))
        self.assertEqual(self.request.call_args_list[2].kwargs['params']['page'], 1)
        self.assertEqual(self.request.call_count, 4)

    async def test_date_boundary_naive_and_offset_dates(self):
        for created, since, count in [
            (CUTOFF.isoformat(), CUTOFF, 1),
            ('2026-10-15T08:00:00-04:00', CUTOFF, 1),
            ('2026-10-15T12:00:00', CUTOFF.replace(tzinfo=None), 1),
            ((CUTOFF - timedelta(seconds=1)).isoformat(), CUTOFF, 0),
        ]:
            with self.subTest(created=created, since=since):
                self.request.side_effect = [response(listing()), response(detail(createdOn=created))]
                self.assertEqual(len(await self.source.fetch(since)), count)

    async def test_filters_match_description_and_exclusions_take_priority(self):
        for filters, expected in [(SearchFilter(['FLUTTER']), 1), (SearchFilter(['missing']), 0),
                                  (SearchFilter(['*'], ['FLUTTER']), 0), (SearchFilter(['*']), 1),
                                  (SearchFilter(['flutter'], ['software company']), 0)]:
            with self.subTest(filters=filters):
                self.request.side_effect = [response(listing()), response(detail())]
                self.assertEqual(len(await self.source_with(filters).fetch(CUTOFF)), expected)
        self.request.reset_mock()
        self.assertEqual(await self.source_with(SearchFilter([])).fetch(CUTOFF), [])
        self.request.assert_not_called()

    async def test_empty_listing_and_removed_postings(self):
        self.request.side_effect = [response(listing([]))]
        self.assertEqual(await self.source.fetch(CUTOFF), [])
        for status in (404, 410):
            with self.subTest(status=status):
                self.request.side_effect = [response(listing()), response(None, status)]
                self.assertEqual(await self.source.fetch(CUTOFF), [])

    async def test_remote_hybrid_onsite_and_unknown(self):
        for workplaces, expected in [(['REMOTE'], True), (['HYBRID'], True), (['ON_SITE'], False),
                                     ([], None), ([None], None), (['ON_SITE', None], None),
                                     (['ON_SITE', 'REMOTE'], True)]:
            with self.subTest(workplaces=workplaces):
                locations = [{'name': 'Example', 'workplaceType': value} for value in workplaces]
                self.request.side_effect = [response(listing([record(locations=locations)])), response(detail())]
                job, = await self.source.fetch(CUTOFF)
                self.assertIs(job.is_remote_allowed, expected)

    async def test_fingerprint_survives_title_changes_and_differs_between_companies(self):
        first, = await self.source.fetch(CUTOFF)
        self.request.side_effect = [response(listing()), response(detail(name='Senior Engineer'))]
        second, = await self.source.fetch(CUTOFF)
        self.assertEqual(first.fingerprint, second.fingerprint)
        other = RipplingJobsSource(RipplingJobsSourceConfig('another-company', SearchFilter()))
        other_job = other._parse_job(detail()['apiData']['jobPost'], record(), board(), JOB_URL, JOB_ID)
        self.assertNotEqual(first.fingerprint, other_job.fingerprint)

    async def test_invalid_listing_and_detail_data_raise_source_error(self):
        for props in [None, {}, listing(items=None), listing(totalPages=5), listing(page=1),
                      listing([record(id='../invalid')]), listing([record(locations='bad')]),
                      listing([record(locations=[{'name': 'X', 'workplaceType': 'BAD'}])]),
                      {'apiData': {'jobBoard': board('another-company')}}]:
            with self.subTest(props=props):
                self.request.side_effect = [response(props), response(detail())]
                with self.assertRaisesRegex(RipplingFetchError, 'invalid job data'):
                    await self.source.fetch(CUTOFF)
        for props in [None, {}, detail(uuid=OTHER_ID), detail(createdOn='bad'), detail(name=''),
                      detail(description={}), detail(description={'role': '<p></p>'}),
                      detail(description={'role': []})]:
            with self.subTest(props=props):
                self.request.side_effect = [response(listing()), response(props)]
                with self.assertRaisesRegex(RipplingFetchError, 'invalid job data'):
                    await self.source.fetch(CUTOFF)

    async def test_repeated_or_incomplete_pages_are_failures(self):
        self.request.side_effect = [response(listing(size=1, total=2)), response(detail()),
                                    response(listing(page=1, size=1, total=2))]
        with self.assertRaisesRegex(RipplingFetchError, 'invalid job data'):
            await self.source.fetch(CUTOFF)
        self.request.side_effect = [response(listing([], total=2))]
        with self.assertRaises(RipplingFetchError):
            await self.source.fetch(CUTOFF)

    async def test_http_network_and_html_failures(self):
        for status in (403, 404, 429, 500):
            with self.subTest(status=status):
                failed = response(None, status)
                error_response = requests.Response()
                error_response.status_code = status
                failed.raise_for_status.side_effect = requests.HTTPError(response=error_response)
                self.request.side_effect = [failed]
                with self.assertRaisesRegex(RipplingFetchError, f'HTTP {status}'):
                    await self.source.fetch(CUTOFF)
        self.request.side_effect = requests.Timeout('timed out')
        with self.assertRaisesRegex(RipplingFetchError, 'network connectivity'):
            await self.source.fetch(CUTOFF)
        for html in ('<html>Blocked</html>', '<script id="__NEXT_DATA__">bad json</script>'):
            self.request.side_effect = [Mock(status_code=200, text=html)]
            with self.assertRaisesRegex(RipplingFetchError, 'invalid job data'):
                await self.source.fetch(CUTOFF)

    async def test_detail_failure_reports_company_and_posting(self):
        self.request.side_effect = [response(listing()), requests.Timeout('timed out')]
        reporter = Mock(spec=ErrorReporter)
        with self.assertRaises(RipplingFetchError):
            await ErrorReportingJobsSource(self.source, reporter).fetch(CUTOFF)
        reporter.report.assert_called_once()
        context = reporter.report.call_args.kwargs['context']
        self.assertEqual(context['source'], 'Rippling: sparq')
        self.assertEqual(context['operation'], 'fetch_posting')
        self.assertEqual(context['posting']['url'], JOB_URL)
