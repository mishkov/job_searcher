import hashlib
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import requests

from src.error_reporting import ErrorReporter
from src.job_sources.error_reporting_source import ErrorReportingJobsSource
from src.job_sources.maxline.maxline import MaxLineFetchError, MaxLineJobsSource, MaxLineJobsSourceConfig
from src.search_filter import SearchFilter


CUTOFF = datetime(2099, 1, 1, tzinfo=timezone.utc)
BASE = MaxLineJobsSource.LISTING_URL


def listing(slugs=('project-manager',)):
    return ('<section><h2>Присоединяйся к команде</h2><div id="vacanciesCarousel">' + ''.join(
        f'<a class="vacancy-card" href="/vacancies/{slug}"><h3>Engineer</h3></a>' for slug in slugs)
        + '</div></section><footer>Flutter unpaid role</footer>')


def detail(title='Engineer', description='Build FLUTTER &amp; Dart apps.'):
    return ('<div class="vacancy-page__container"><h1>Вакансия</h1>'
            f'<h2 class="vacancy-page__title">{title}</h2>'
            '<ul class="vacancy-page__summary"><li>3+ years</li></ul>'
            '<div class="vacancy-page__block"><h3>Requirements</h3>'
            f'<ul class="vacancy-page__list"><li>{description}</li></ul></div></div>'
            '<form>Unpaid cookies and CV</form><footer>Other jobs</footer>')


def response(html, status=200):
    result = Mock(text=html, status_code=status)
    if status >= 400:
        error_response = requests.Response()
        error_response.status_code = status
        result.raise_for_status.side_effect = requests.HTTPError(response=error_response)
    return result


class MaxLineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patcher = patch('src.job_sources.maxline.maxline.requests.get')
        self.request = patcher.start()
        self.addCleanup(patcher.stop)
        self.source = self.source_with(SearchFilter(['flutter'], ['unpaid']))
        self.request.side_effect = [response(listing()), response(detail())]

    @staticmethod
    def source_with(filters):
        return MaxLineJobsSource(MaxLineJobsSourceConfig(filters))

    async def test_full_description_metadata_stable_fingerprint_and_undated_jobs(self):
        job, = await self.source.fetch(CUTOFF)
        self.assertEqual(job.title, 'Engineer')
        for text in ('3+ years', 'Requirements', 'Build FLUTTER & Dart apps.',
                     'Company: MaxLine', 'Location: Минск', 'Workplace: Office'):
            self.assertIn(text, job.description)
        for text in ('<li>', 'cookies', 'Other jobs', 'CV'):
            self.assertNotIn(text, job.description)
        self.assertIs(job.is_remote_allowed, False)
        self.assertEqual(job.salary, '-')
        self.assertEqual(job.url, f'{BASE}vacancies/project-manager/')
        self.assertEqual(job.fingerprint, hashlib.sha256(b'hr.maxline.by:project-manager').hexdigest())
        self.request.assert_any_call(job.url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=30)
        self.request.side_effect = [response(listing()), response(detail(title='New title'))]
        renamed, = await self.source.fetch(CUTOFF.replace(tzinfo=None))
        self.assertEqual(renamed.fingerprint, job.fingerprint)

    async def test_filters_wildcard_union_exclusions_and_empty_keywords(self):
        for filters, description, expected in (
            (SearchFilter(['*']), 'Java role', 1),
            (SearchFilter(['python', 'DART']), 'Dart role', 1),
            (SearchFilter(['python']), 'Dart role', 0),
            (SearchFilter(['*'], ['UNPAID']), 'Unpaid role', 0),
            (SearchFilter(['flutter'], ['UNPAID']), 'Flutter unpaid role', 0),
        ):
            with self.subTest(filters=filters):
                self.request.side_effect = [response(listing()), response(detail(description=description))]
                self.assertEqual(len(await self.source_with(filters).fetch(CUTOFF)), expected)
        self.request.reset_mock()
        self.assertEqual(await self.source_with(SearchFilter([])).fetch(CUTOFF), [])
        self.request.assert_not_called()

    async def test_duplicate_links_empty_listing_and_fresh_next_run(self):
        self.request.side_effect = [response(listing(('project-manager', 'project-manager', 'dba'))),
                                    response(detail()), response(detail())]
        self.assertEqual(len(await self.source.fetch(CUTOFF)), 2)
        self.assertEqual(self.request.call_count, 3)
        self.request.side_effect = [response(listing(())), response(listing()), response(detail())]
        self.assertEqual(await self.source.fetch(CUTOFF), [])
        self.assertEqual(len(await self.source.fetch(CUTOFF)), 1)

    async def test_removed_postings_are_skipped(self):
        for status in (404, 410):
            self.request.side_effect = [response(listing(('project-manager', 'dba'))),
                                        response('', status), response(detail())]
            job, = await self.source.fetch(CUTOFF)
            self.assertEqual(job.url, f'{BASE}vacancies/dba/')

    async def test_malformed_listing_and_detail_fail(self):
        for html in ('Maintenance', '<section id="vacancies">Unavailable</section>',
                     listing().replace('/vacancies/project-manager', 'https://evil.example/vacancies/test')):
            self.request.side_effect = [response(html)]
            with self.assertRaisesRegex(MaxLineFetchError, 'invalid job data'):
                await self.source.fetch(CUTOFF)
        for html in ('Maintenance', detail(title=''), detail(description='').replace('<li></li>', ''),
                     detail().replace('vacancy-page__block', 'missing')):
            self.request.side_effect = [response(listing()), response(html)]
            with self.assertRaisesRegex(MaxLineFetchError, 'invalid job data'):
                await self.source.fetch(CUTOFF)

    async def test_http_and_network_errors_with_posting_context(self):
        for status in (403, 404, 429, 500):
            for on_detail in (False, True):
                if on_detail and status == 404:
                    continue
                self.request.side_effect = ([response(listing())] if on_detail else []) + [response('', status)]
                with self.assertRaisesRegex(MaxLineFetchError, f'HTTP {status}') as raised:
                    await self.source.fetch(CUTOFF)
                if status == 429:
                    self.assertIn('rate limit exceeded', str(raised.exception))
        reporter = Mock(spec=ErrorReporter)
        self.request.side_effect = [response(listing()), requests.Timeout()]
        with self.assertRaisesRegex(MaxLineFetchError, 'network connectivity'):
            await ErrorReportingJobsSource(self.source, reporter).fetch(CUTOFF)
        context = reporter.report.call_args.kwargs['context']
        self.assertEqual(context['source'], 'MaxLine')
        self.assertEqual(context['operation'], 'fetch_posting')
        self.assertEqual(context['posting'], {'id': 'project-manager', 'url': f'{BASE}vacancies/project-manager/'})

    def test_canonical_urls_and_validation(self):
        expected = (f'{BASE}vacancies/dba/', 'dba')
        for url in ('/vacancies/dba', f'{BASE}vacancies/dba/?tracking=1#apply'):
            self.assertEqual(self.source._job_url(url), expected)
        for url in (None, 'https://evil.example/vacancies/dba', '/vacancies/', '/other/dba',
                    'http://hr.maxline.by/vacancies/dba', '/vacancies/dba/extra'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.source._job_url(url)
