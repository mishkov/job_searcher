import hashlib
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import requests

from src.error_reporting import ErrorReporter
from src.job_sources.aj_bell.aj_bell import AjBellFetchError, AjBellJobsSource, AjBellJobsSourceConfig
from src.job_sources.error_reporting_source import ErrorReportingJobsSource
from src.search_filter import SearchFilter


CUTOFF = datetime(2026, 10, 26, tzinfo=timezone.utc)
BASE = AjBellJobsSource.LISTING_URL


def listing(ids=(744000155695786,), next_page=None):
    cards = ''.join(f'<div><a href="/group/techhub/vacancies/{identity}">'
                    '<div class="card-career"><h3 class="uk-card-title">Engineer</h3>'
                    '<div class="item-icon">London / Hybrid</div></div></a></div>' for identity in ids)
    pager = f'<li class="pager__item--next"><a href="{next_page}">Next</a></li>' if next_page else ''
    return ('<main><div class="block-views-blockcareers-techhub"><div class="views-view">'
            f'<div id="views-uikit-careers-techhub"><div data-uk-grid>{cards}</div></div>{pager}'
            '</div></div></main><footer><a href="/group/careers/vacancies/999">Other job</a></footer>')


def detail(title='Engineer', description='<p>Build FLUTTER &amp; Dart apps.</p>', location='London / Hybrid'):
    return ('<main><h1 class="uk-article-title">' + title + '</h1>'
            '<div class="block-views-blockvacancy-vacancy"><div class="views-view"><div>'
            f'<div class="field-content">{description}</div><iframe title="Life at AJ Bell"></iframe>'
            '<a href="https://jobs.smartrecruiters.com/AJBell1/123-engineer">Apply Now</a></div></div></div>'
            '<aside><div class="block-views-blockvacancy-meta">'
            f'<div class="item-icon"><span uk-icon="location-pin"></span> {location}</div>'
            '<div class="item-icon"><span uk-icon="briefcase"></span> Software Development</div></div>'
            '<div class="block-views-blockcareers-featured-techhub">Unpaid featured role</div></aside></main>'
            '<footer>Unpaid cookie policy</footer>')


def response(html, status=200):
    result = Mock(text=html, status_code=status)
    if status >= 400:
        error_response = requests.Response()
        error_response.status_code = status
        result.raise_for_status.side_effect = requests.HTTPError(response=error_response)
    return result


class AjBellTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patcher = patch('src.job_sources.aj_bell.aj_bell.requests.get')
        self.request = patcher.start()
        self.addCleanup(patcher.stop)
        self.source = self.source_with(SearchFilter(['flutter'], ['unpaid']))
        self.request.side_effect = [response(listing()), response(detail())]

    @staticmethod
    def source_with(filters):
        return AjBellJobsSource(AjBellJobsSourceConfig(filters))

    async def test_live_shaped_full_posting_and_stable_fingerprint(self):
        job, = await self.source.fetch(CUTOFF)
        self.assertEqual(job.title, 'Engineer')
        for text in ('Build FLUTTER & Dart apps.', 'Company: AJ Bell', 'Location: London / Hybrid',
                     'Department: Software Development'):
            self.assertIn(text, job.description)
        for text in ('<p>', 'cookie', 'featured', 'Apply Now', 'Life at AJ Bell'):
            self.assertNotIn(text, job.description)
        self.assertEqual(job.salary, '-')
        self.assertIs(job.is_remote_allowed, True)
        self.assertEqual(job.url, f'{BASE}/744000155695786')
        self.assertEqual(job.fingerprint, hashlib.sha256(b'ajbell.co.uk:744000155695786').hexdigest())
        self.request.assert_any_call(BASE, headers={'User-Agent': 'Mozilla/5.0'}, timeout=30)
        self.request.assert_any_call(job.url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=30)
        self.request.side_effect = [response(listing()), response(detail(title='Renamed Engineer'))]
        renamed, = await self.source.fetch(CUTOFF)
        self.assertEqual(renamed.fingerprint, job.fingerprint)

    async def test_undated_jobs_remain_eligible_on_every_run(self):
        for since in (CUTOFF, datetime(2099, 1, 1), datetime(2099, 1, 1, tzinfo=timezone.utc)):
            with self.subTest(since=since):
                self.request.side_effect = [response(listing()), response(detail())]
                self.assertEqual(len(await self.source.fetch(since)), 1)

    async def test_full_text_keyword_union_wildcard_and_exclusion_priority(self):
        for filters, description, expected in (
            (SearchFilter(['*']), 'Java role', 1),
            (SearchFilter(['python', 'DART']), '<p>Build Dart apps</p>', 1),
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

    async def test_duplicate_ids_and_pagination(self):
        self.request.side_effect = [response(listing((1, 1, 2), next_page='?page=1')), response(detail()),
                                   response(detail()), response(listing((2, 3))), response(detail())]
        jobs = await self.source.fetch(CUTOFF)
        self.assertEqual([job.url.rsplit('/', 1)[-1] for job in jobs], ['1', '2', '3'])
        self.request.assert_any_call(f'{BASE}?page=1', headers={'User-Agent': 'Mozilla/5.0'}, timeout=30)
        self.assertEqual(self.request.call_count, 5)

    async def test_empty_listing_is_success_and_next_run_is_fresh(self):
        self.request.side_effect = [response(listing(())), response(listing()), response(detail())]
        self.assertEqual(await self.source.fetch(CUTOFF), [])
        self.assertEqual(len(await self.source.fetch(CUTOFF)), 1)

    async def test_removed_jobs_are_skipped(self):
        for status in (404, 410):
            with self.subTest(status=status):
                self.request.side_effect = [response(listing((1, 2))), response('', status), response(detail())]
                job, = await self.source.fetch(CUTOFF)
                self.assertEqual(job.url, f'{BASE}/2')

    async def test_optional_location_and_explicit_workplace_only(self):
        for location, expected in (('London / Hybrid', True), ('UK / REMOTE', True), ('Manchester', None), ('', None)):
            with self.subTest(location=location):
                self.request.side_effect = [response(listing()), response(detail(
                    location=location, description='Flutter remote work, salary GBP 80,000 annually'))]
                job, = await self.source.fetch(CUTOFF)
                self.assertIs(job.is_remote_allowed, expected)
                self.assertEqual(job.salary, '-')
                self.assertIn('GBP 80,000 annually', job.description)
                if not location:
                    self.assertNotIn('Location:', job.description)
        self.request.side_effect = [response(listing()), response(detail().replace('block-views-blockvacancy-meta', 'other'))]
        job, = await self.source.fetch(CUTOFF)
        self.assertIsNone(job.is_remote_allowed)

    async def test_malformed_listing_and_repeated_or_invalid_next_page_fail(self):
        for html in ('<html>Maintenance</html>', '<div class="block-views-blockcareers-techhub"></div>',
                     listing().replace('/group/techhub/vacancies/', '//evil.example/'),
                     listing((), next_page='https://evil.example/?page=1'),
                     listing((), next_page='?page=0')):
            with self.subTest(html=html):
                self.request.side_effect = [response(html)]
                with self.assertRaisesRegex(AjBellFetchError, 'invalid job data'):
                    await self.source.fetch(CUTOFF)
        self.request.side_effect = [response(listing((), next_page='?page=1')),
                                   response(listing((), next_page='?page=1'))]
        with self.assertRaisesRegex(AjBellFetchError, 'invalid job data'):
            await self.source.fetch(CUTOFF)

    async def test_invalid_postings_fail(self):
        for html in ('<html>Maintenance</html>', detail(title=''), detail(description=''),
                     detail().replace('field-content', 'unrelated-content')):
            with self.subTest(html=html):
                self.request.side_effect = [response(listing()), response(html)]
                with self.assertRaisesRegex(AjBellFetchError, 'invalid job data'):
                    await self.source.fetch(CUTOFF)

    async def test_http_failures_and_rate_limits_on_listing_and_detail(self):
        for status in (403, 404, 429, 500):
            for on_detail in (False, True):
                if on_detail and status == 404:
                    continue
                with self.subTest(status=status, on_detail=on_detail):
                    self.request.side_effect = ([response(listing())] if on_detail else []) + [response('', status)]
                    with self.assertRaisesRegex(AjBellFetchError, f'HTTP {status}') as raised:
                        await self.source.fetch(CUTOFF)
                    if status == 429:
                        self.assertIn('rate limit exceeded', str(raised.exception))

    async def test_network_failures_and_context(self):
        for on_detail in (False, True):
            with self.subTest(on_detail=on_detail):
                reporter = Mock(spec=ErrorReporter)
                self.request.side_effect = ([response(listing())] if on_detail else []) + [requests.Timeout()]
                with self.assertRaisesRegex(AjBellFetchError, 'network connectivity'):
                    await ErrorReportingJobsSource(self.source, reporter).fetch(CUTOFF)
                reporter.report.assert_called_once()
                context = reporter.report.call_args.kwargs['context']
                self.assertEqual(context['source'], 'AJ Bell techHub')
                self.assertEqual(context['operation'], 'fetch_posting' if on_detail else 'fetch_listing')
                self.assertEqual(context['page'], 0)
                if on_detail:
                    self.assertEqual(context['posting'], {'id': '744000155695786', 'url': f'{BASE}/744000155695786'})
                else:
                    self.assertIsNone(context['posting'])

    async def test_later_page_failure_clears_posting_context(self):
        reporter = Mock(spec=ErrorReporter)
        self.request.side_effect = [response(listing(next_page='?page=1')), response(detail()), requests.ConnectionError()]
        with self.assertRaises(AjBellFetchError):
            await ErrorReportingJobsSource(self.source, reporter).fetch(CUTOFF)
        context = reporter.report.call_args.kwargs['context']
        self.assertEqual(context['operation'], 'fetch_listing')
        self.assertEqual(context['page'], 1)
        self.assertIsNone(context['posting'])

    def test_job_and_pagination_url_validation(self):
        self.assertEqual(self.source._job_url(f'{BASE}/123/?tracking=1#title'), (f'{BASE}/123', '123'))
        for value in (None, 'https://evil.example/123', '/group/careers/vacancies/123',
                      'http://www.ajbell.co.uk/group/techhub/vacancies/123', BASE):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.source._job_url(value)
        for value in (None, '?search=flutter', '?page=', '?page=-1', '?page=1&page=2', '/other?page=1'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.source._next_page_url(value)
