import hashlib
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import requests

from src.error_reporting import ErrorReporter
from src.job_sources.brightstar_lottery.brightstar_lottery import (
    BrightstarLotteryFetchError, BrightstarLotteryJobsSource, BrightstarLotteryJobsSourceConfig,
)
from src.job_sources.error_reporting_source import ErrorReportingJobsSource
from src.search_filter import SearchFilter


CUTOFF = datetime(2026, 10, 22, 2, 1, tzinfo=timezone.utc)
BASE = BrightstarLotteryJobsSource.BASE_URL


def listing(ids=(1440621533,), total=None, page_size=25, initial=True):
    # Match the live site's JS settings and tile markup, including duplicate
    # title links for desktop/tablet/mobile. aria-rowcount is not the total.
    config = (f'<script>j2w.SearchResults.init({{jobRecordsPerPage: parseInt("{page_size}"), '
              f'jobRecordsFound: parseInt("{len(ids) if total is None else total}")}});</script>') if initial else ''
    tiles = ''.join(f'<li class="job-tile" data-url="/job/Warsaw-Engineer/{identity}/">'
                    + f'<a class="jobTitle-link" href="/job/Warsaw-Engineer/{identity}/">Engineer</a>' * 3
                    + '</li>' for identity in ids)
    return f'{config}<ul id="job-tile-list" aria-rowcount="0">{tiles}</ul>'


def detail(title='Engineer', description='<h2>Requirements</h2><p>Build FLUTTER &amp; Dart apps.</p>',
           date='Thu Oct 22 02:01:00 UTC 2026', location='Warsaw, PL, 00-807'):
    return (f'<meta itemprop="datePosted" content="{date}">'
            f'<h1><span itemprop="title">{title}</span></h1>'
            f'<p id="job-location">{location}</p>'
            f'<span itemprop="description">{description}</span>'
            '<footer>Unpaid cookie policy</footer>')


def response(html, status=200):
    result = Mock(text=html, status_code=status)
    if status >= 400:
        error_response = requests.Response()
        error_response.status_code = status
        result.raise_for_status.side_effect = requests.HTTPError(response=error_response)
    return result


class BrightstarLotteryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patcher = patch('src.job_sources.brightstar_lottery.brightstar_lottery.requests.get')
        self.request = patcher.start()
        self.addCleanup(patcher.stop)
        self.source = self.source_with(SearchFilter(['flutter'], ['unpaid']))
        self.request.side_effect = [response(listing()), response(detail())]

    @staticmethod
    def source_with(filters):
        return BrightstarLotteryJobsSource(BrightstarLotteryJobsSourceConfig(filters))

    async def test_live_shaped_posting_full_description_and_stable_fingerprint(self):
        job, = await self.source.fetch(CUTOFF)
        self.assertEqual(job.title, 'Engineer')
        self.assertIn('Build FLUTTER & Dart apps.', job.description)
        self.assertIn('Company: Brightstar Lottery', job.description)
        self.assertIn('Location: Warsaw, PL, 00-807', job.description)
        self.assertNotIn('<p>', job.description)
        self.assertNotIn('cookie', job.description)
        self.assertEqual(job.salary, '-')
        self.assertIsNone(job.is_remote_allowed)
        self.assertEqual(job.url, f'{BASE}/job/Warsaw-Engineer/1440621533/')
        self.assertEqual(job.fingerprint, hashlib.sha256(b'careers.brightstarlottery.com:1440621533').hexdigest())
        self.request.assert_any_call(job.url, params=None, timeout=30)
        self.request.side_effect = [response(listing()), response(detail(title='Renamed Engineer'))]
        renamed, = await self.source.fetch(CUTOFF)
        self.assertEqual(renamed.fingerprint, job.fingerprint)

    async def test_all_pages_unsorted_dates_and_duplicate_ids(self):
        self.request.side_effect = [response(listing((1, 2), total=5, page_size=2)),
                                   response(detail(date='Wed Oct 21 02:01:00 UTC 2026')), response(detail()),
                                   response(listing((2, 3), initial=False)), response(detail()),
                                   response(listing((4,), initial=False)), response(detail())]
        jobs = await self.source.fetch(CUTOFF)
        self.assertEqual([job.url.split('/')[-2] for job in jobs], ['2', '3', '4'])
        pages = [call for call in self.request.call_args_list if '/tile-search-results/' in call.args[0]]
        self.assertEqual([call.kwargs['params']['startrow'] for call in pages], [2, 4])
        self.assertTrue(all(call.kwargs['params']['locale'] == 'en_US' for call in pages))

    async def test_inclusive_utc_cutoff_and_naive_datetimes(self):
        for cutoff, expected in ((CUTOFF, 1), (CUTOFF.replace(tzinfo=None), 1),
                                 (CUTOFF.astimezone(timezone(timedelta(hours=3))), 1),
                                 (CUTOFF + timedelta(seconds=1), 0)):
            with self.subTest(cutoff=cutoff):
                self.request.side_effect = [response(listing()), response(detail())]
                self.assertEqual(len(await self.source.fetch(cutoff)), expected)

    async def test_wildcard_or_keywords_and_exclusions_in_full_text(self):
        for filters, description, expected in (
            (SearchFilter(['*']), 'Java role', 1),
            (SearchFilter(['python', 'DART']), 'Build Dart apps', 1),
            (SearchFilter(['python']), 'Build Dart apps', 0),
            (SearchFilter(['*'], ['UNPAID']), 'Flutter unpaid role', 0),
            (SearchFilter(['flutter'], ['UNPAID']), 'Flutter unpaid role', 0),
        ):
            with self.subTest(filters=filters):
                self.request.side_effect = [response(listing()), response(detail(description=description))]
                self.assertEqual(len(await self.source_with(filters).fetch(CUTOFF)), expected)

    async def test_empty_keywords_skip_network(self):
        self.assertEqual(await self.source_with(SearchFilter([])).fetch(CUTOFF), [])
        self.request.assert_not_called()

    async def test_empty_listing_is_success_and_next_run_is_fresh(self):
        self.request.side_effect = [response(listing(())), response(listing()), response(detail())]
        self.assertEqual(await self.source.fetch(CUTOFF), [])
        self.assertEqual(len(await self.source.fetch(CUTOFF)), 1)

    async def test_missing_optional_location_and_no_inferred_remote_or_salary(self):
        self.request.side_effect = [response(listing()), response(detail(
            location='', description='Flutter remote work, salary USD 100,000 annually'))]
        job, = await self.source.fetch(CUTOFF)
        self.assertNotIn('Location:', job.description)
        self.assertIn('USD 100,000 annually', job.description)
        self.assertIsNone(job.is_remote_allowed)

    async def test_removed_jobs_are_skipped(self):
        for status in (404, 410):
            with self.subTest(status=status):
                self.request.side_effect = [response(listing()), response('', status)]
                self.assertEqual(await self.source.fetch(CUTOFF), [])

    async def test_filled_job_returning_http_200_is_skipped(self):
        closed = ('<div class="jobDisplayShell"><div class="jobDisplay"><div class="content">'
                  '<div class="jobTitle"></div><div class="job">'
                  '<p><strong>Sorry, this position has been filled.</strong></p></div></div></div></div>')
        self.request.side_effect = [response(listing((1, 2))), response(closed), response(detail())]
        job, = await self.source.fetch(CUTOFF)
        self.assertTrue(job.url.endswith('/2/'))

    async def test_malformed_or_incomplete_listing_fails(self):
        for html in ('<html>Maintenance</html>', listing((1,), total=2),
                     listing((), page_size=0), listing().replace('/job/Warsaw-Engineer/', '//evil.example/job/x/')):
            with self.subTest(html=html):
                self.request.side_effect = [response(html)]
                with self.assertRaisesRegex(BrightstarLotteryFetchError, 'invalid job data'):
                    await self.source.fetch(CUTOFF)

    async def test_repeated_or_missing_later_page_fails(self):
        for html in (listing((1,), initial=False), '<html>Maintenance</html>'):
            self.request.side_effect = [response(listing((1,), total=2, page_size=1)),
                                       response(detail()), response(html)]
            with self.assertRaisesRegex(BrightstarLotteryFetchError, 'invalid job data'):
                await self.source.fetch(CUTOFF)

    async def test_invalid_posting_fails(self):
        for html in ('<html>Maintenance</html>', detail(date='invalid'), detail(title=''),
                     detail(description=''), detail().replace('itemprop="title"', 'class="title"')):
            with self.subTest(html=html):
                self.request.side_effect = [response(listing()), response(html)]
                with self.assertRaisesRegex(BrightstarLotteryFetchError, 'invalid job data'):
                    await self.source.fetch(CUTOFF)

    async def test_http_failures_and_rate_limit_on_listing_and_detail(self):
        for status in (403, 404, 429, 500):
            for on_detail in (False, True):
                if on_detail and status == 404:
                    continue
                with self.subTest(status=status, on_detail=on_detail):
                    self.request.side_effect = ([response(listing())] if on_detail else []) + [response('', status)]
                    with self.assertRaisesRegex(BrightstarLotteryFetchError, f'HTTP {status}') as raised:
                        await self.source.fetch(CUTOFF)
                    if status == 429:
                        self.assertIn('rate limit exceeded', str(raised.exception))

    async def test_detail_network_failure_reports_posting_context(self):
        reporter = Mock(spec=ErrorReporter)
        self.request.side_effect = [response(listing()), requests.Timeout('timed out')]
        with self.assertRaisesRegex(BrightstarLotteryFetchError, 'network connectivity'):
            await ErrorReportingJobsSource(self.source, reporter).fetch(CUTOFF)
        reporter.report.assert_called_once()
        context = reporter.report.call_args.kwargs['context']
        self.assertEqual(context['operation'], 'fetch_posting')
        self.assertEqual(context['posting']['id'], '1440621533')
        self.assertEqual(context['page'], 0)

    async def test_later_page_failure_clears_posting_context(self):
        reporter = Mock(spec=ErrorReporter)
        self.request.side_effect = [response(listing((1,), total=2, page_size=1)),
                                   response(detail()), requests.ConnectionError()]
        with self.assertRaises(BrightstarLotteryFetchError):
            await ErrorReportingJobsSource(self.source, reporter).fetch(CUTOFF)
        context = reporter.report.call_args.kwargs['context']
        self.assertEqual(context['operation'], 'fetch_listing')
        self.assertEqual(context['page'], 1)
        self.assertIsNone(context['posting'])

    def test_url_validation_and_tracking_removal(self):
        url, identity = self.source._job_url(f'{BASE}/job/Warsaw-Engineer/123/?locale=en_US#title')
        self.assertEqual(url, f'{BASE}/job/Warsaw-Engineer/123/')
        self.assertEqual(identity, '123')
        for value in (None, '//evil.example/job/Engineer/123/', '/search/',
                      'http://careers.brightstarlottery.com/job/Engineer/123/'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.source._job_url(value)
