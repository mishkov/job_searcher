import hashlib
import json
import unittest
from datetime import datetime, timezone
from html import escape
from unittest.mock import Mock, patch

import requests

from src.error_reporting import ErrorReporter
from src.job_sources.error_reporting_source import ErrorReportingJobsSource
from src.job_sources.hackmotion.hackmotion import HackMotionFetchError, HackMotionJobsSource, HackMotionJobsSourceConfig
from src.search_filter import SearchFilter


CUTOFF = datetime(2026, 10, 16, 7, 16, 30, tzinfo=timezone.utc)
BASE = HackMotionJobsSource.LISTING_URL


def listing(ids=(8555653,), next_page=None):
    links = ''.join(f'<li><a href="{BASE}/{identity}-developer">Mobile Developer</a></li>' for identity in ids)
    pager = f'<a rel="next" href="{next_page}">Next</a>' if next_page else ''
    return (f'<turbo-frame id="jobs_list"><ul id="jobs_list_container">{links}</ul>{pager}</turbo-frame>'
            '<footer>Unpaid cookie policy</footer>')


def detail(**changes):
    record = {'@context': 'http://schema.org/', '@type': 'JobPosting', 'title': 'Senior Mobile Developer',
              'description': escape('<h1>About this position</h1><p>Build FLUTTER &amp; Dart apps.</p>'
                                    '<p>Salary 4000 - 4800 EUR before taxes + stock options.</p>'),
              'datePosted': '2026-10-16T10:16:30+03:00',
              'identifier': {'@type': 'PropertyValue', 'name': 'HackMotion', 'value': '8555653'}}
    record.update(changes)
    return ('<script type="application/ld+json">' + json.dumps(record) + '</script>'
            '<dl><dt>Location</dt><dd>Rīga</dd><dt>Remote status</dt><dd>Hybrid</dd></dl>'
            '<footer>Unpaid cookie policy</footer>')


def response(html, status=200):
    result = Mock(text=html, status_code=status)
    if status >= 400:
        error_response = requests.Response()
        error_response.status_code = status
        result.raise_for_status.side_effect = requests.HTTPError(response=error_response)
    return result


class HackMotionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patcher = patch('src.job_sources.hackmotion.hackmotion.requests.get')
        self.request = patcher.start()
        self.addCleanup(patcher.stop)
        self.source = self.source_with(SearchFilter(['flutter'], ['unpaid']))
        self.request.side_effect = [response(listing()), response(detail())]

    @staticmethod
    def source_with(filters):
        return HackMotionJobsSource(HackMotionJobsSourceConfig(filters))

    async def test_live_shaped_posting_and_stable_identity(self):
        job, = await self.source.fetch(CUTOFF)
        self.assertEqual(job.title, 'Senior Mobile Developer')
        for text in ('Build FLUTTER & Dart apps.', 'Company: HackMotion', 'Location: Rīga', 'Remote status: Hybrid'):
            self.assertIn(text, job.description)
        for text in ('<p>', '&amp;', 'cookie'):
            self.assertNotIn(text, job.description)
        self.assertEqual(job.salary, '4000 - 4800 EUR before taxes + stock options.')
        self.assertIs(job.is_remote_allowed, True)
        self.assertEqual(job.url, f'{BASE}/8555653-developer')
        self.assertEqual(job.fingerprint, hashlib.sha256(b'jobs.hackmotion.com:8555653').hexdigest())
        self.request.assert_any_call(BASE, headers={'User-Agent': 'Mozilla/5.0'}, timeout=30)
        self.request.side_effect = [response(listing()), response(detail(title='Renamed'))]
        renamed, = await self.source.fetch(CUTOFF)
        self.assertEqual(renamed.fingerprint, job.fingerprint)

    async def test_dates_are_inclusive_and_timezone_aware(self):
        for posted, since, expected in (
            ('2026-10-16T10:16:30+03:00', CUTOFF, 1),
            ('2026-10-16T07:16:29Z', CUTOFF, 0),
            ('2026-10-16T07:16:31Z', CUTOFF.replace(tzinfo=None), 1),
            ('2026-10-16', CUTOFF, 1),
            ('2026-10-15', CUTOFF, 0),
        ):
            with self.subTest(posted=posted):
                self.request.side_effect = [response(listing()), response(detail(datePosted=posted))]
                self.assertEqual(len(await self.source.fetch(since)), expected)

    async def test_full_description_filters_and_exclusion_priority(self):
        for filters, expected in (
            (SearchFilter(['*']), 1), (SearchFilter(['python', 'DART']), 1),
            (SearchFilter(['python']), 0), (SearchFilter(['flutter'], ['STOCK OPTIONS']), 0),
            (SearchFilter(['*'], ['FLUTTER']), 0),
        ):
            with self.subTest(filters=filters):
                self.request.side_effect = [response(listing()), response(detail())]
                self.assertEqual(len(await self.source_with(filters).fetch(CUTOFF)), expected)
        self.request.reset_mock()
        self.assertEqual(await self.source_with(SearchFilter([])).fetch(CUTOFF), [])
        self.request.assert_not_called()

    async def test_pagination_deduplication_and_fresh_listing(self):
        self.request.side_effect = [response(listing((1, 1), '?page=2')), response(detail()),
                                   response(listing((1, 2))), response(detail()), response(listing(()))]
        jobs = await self.source.fetch(CUTOFF)
        self.assertEqual([job.url for job in jobs], [f'{BASE}/1-developer', f'{BASE}/2-developer'])
        self.request.assert_any_call(f'{BASE}?page=2', headers={'User-Agent': 'Mozilla/5.0'}, timeout=30)
        self.assertEqual(await self.source.fetch(CUTOFF), [])

    async def test_removed_jobs_are_skipped(self):
        for status in (404, 410):
            self.request.side_effect = [response(listing((1, 2))), response('', status), response(detail())]
            job, = await self.source.fetch(CUTOFF)
            self.assertEqual(job.url, f'{BASE}/2-developer')

    async def test_remote_metadata_and_missing_salary(self):
        for status, expected in (('Fully remote', True), ('No remote work', False), ('Unknown', None)):
            self.request.side_effect = [response(listing()),
                                       response(detail(description='<p>Flutter role</p>').replace('Hybrid', status))]
            job, = await self.source.fetch(CUTOFF)
            self.assertIs(job.is_remote_allowed, expected)
            self.assertEqual(job.salary, '-')
        self.request.side_effect = [response(listing()), response(detail(jobLocationType='TELECOMMUTE').replace('Hybrid', ''))]
        job, = await self.source.fetch(CUTOFF)
        self.assertIs(job.is_remote_allowed, True)

    async def test_invalid_listing_and_pagination_fail(self):
        for html in ('<html>Maintenance</html>', '<turbo-frame id="jobs_list"></turbo-frame>',
                     listing().replace('https://jobs.hackmotion.com/jobs/8555653-developer', 'https://evil.example/jobs/1-role'),
                     listing((), '?page=1'), listing((), 'https://evil.example/?page=2')):
            with self.subTest(html=html):
                self.request.side_effect = [response(html)]
                with self.assertRaisesRegex(HackMotionFetchError, 'invalid job data'):
                    await self.source.fetch(CUTOFF)
        self.request.side_effect = [response(listing((), '?page=2')), response(listing((), '?page=2'))]
        with self.assertRaises(HackMotionFetchError):
            await self.source.fetch(CUTOFF)

    async def test_invalid_postings_fail_instead_of_advancing_history(self):
        for html in ('<html>Maintenance</html>', '<script type="application/ld+json">bad json</script>',
                     detail(title=''), detail(description=''), detail(description='<p></p>'),
                     detail(datePosted='not a date'), detail(datePosted=None), detail(**{'@type': 'Organization'})):
            with self.subTest(html=html):
                self.request.side_effect = [response(listing()), response(html)]
                with self.assertRaisesRegex(HackMotionFetchError, 'invalid job data'):
                    await self.source.fetch(CUTOFF)

    async def test_http_and_network_errors_include_context(self):
        for on_detail in (False, True):
            for status in (403, 429, 500, None):
                with self.subTest(on_detail=on_detail, status=status):
                    reporter = Mock(spec=ErrorReporter)
                    failure = response('', status) if status else requests.Timeout()
                    self.request.side_effect = ([response(listing())] if on_detail else []) + [failure]
                    with self.assertRaises(HackMotionFetchError) as raised:
                        await ErrorReportingJobsSource(self.source, reporter).fetch(CUTOFF)
                    self.assertIn(f'HTTP {status}' if status else 'network connectivity', str(raised.exception))
                    if status == 429:
                        self.assertIn('rate limit exceeded', str(raised.exception))
                    context = reporter.report.call_args.kwargs['context']
                    self.assertEqual(context['operation'], 'fetch_posting' if on_detail else 'fetch_listing')
                    self.assertEqual(context['page'], 0)
                    self.assertEqual(context['source'], 'HackMotion')
                    self.assertEqual(context['posting']['id'] if on_detail else context['posting'],
                                     '8555653' if on_detail else None)

    def test_url_validation(self):
        self.assertEqual(self.source._job_url(f'{BASE}/123-role/?tracking=1#title'), (f'{BASE}/123-role', '123'))
        for value in (None, 'http://jobs.hackmotion.com/jobs/123-role', '/jobs/not-an-id', '/people/123-role'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.source._job_url(value)
        for value in (None, '?page=', '?page=-1', '?page=2&page=3', '?search=flutter', '/other?page=2'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.source._next_page_url(value)
