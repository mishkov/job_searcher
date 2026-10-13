import hashlib
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import requests

from src.job_sources.relocate_me.api import RelocateMeApi, RelocateMeFetchError
from src.job_sources.relocate_me.python_requests_api import PythonRequestsModuleRelocateMeApi
from src.job_sources.relocate_me.relocate_me import RelocateMeJobsSource, RelocateMeJobsSourceConfig
from src.search_filter import SearchFilter


CUTOFF = datetime(2026, 10, 12, 10, tzinfo=timezone.utc)
PATH = '/japan/tokyo/example/mobile-engineer-12345'
URL = 'https://relocate.me' + PATH
OTHER_PATH = '/netherlands/amsterdam/example/engineer-67890'
OTHER_URL = 'https://relocate.me' + OTHER_PATH
PROMO = '/remote/remote/the-global-move/curated-jobs-paid-option-10080'


def posting(**overrides):
    data = {'id': '12345', 'title': 'Mobile Engineer', 'description': 'Build Flutter apps.',
            'date_posted': '2026-10-12', 'salary': '-', 'remote': None}
    return {**data, **overrides}


def listing_html(paths=(PATH,), *, next_page=None, count=None, featured=False):
    count = len(paths) if count is None else count
    cards = ''.join(f'<div class="jobs-list__job {"job_featured" if featured else ""}">'
                    f'<div class="job__title"><a href="{path}">Engineer</a></div></div>' for path in paths)
    pagination = f'<a aria-label="Next page" href="/international-jobs?page={next_page}">Next</a>' if next_page else ''
    return f'<h1>{count} jobs available</h1><div class="jobs-list">{cards}</div>{pagination}'


def detail_html(*, schema_overrides=None, description='Build FLUTTER apps &amp; platforms.',
                location='Tokyo, Japan', archived=False, salary='', malformed=False, schema_wrapper=None):
    schema = {'@type': 'JobPosting', 'title': 'Mobile Engineer\n\t| Relocation Offered',
              'identifier': {'value': '12345'}, 'datePosted': '2026-10-12',
              'employmentType': 'FULL_TIME'}
    schema.update(schema_overrides or {})
    if schema_wrapper:
        schema = schema_wrapper(schema)
    script = json.dumps(schema)
    if malformed:
        script = script.replace('\\n', '\n').replace('\\t', '\t')
    return f'''<script type="application/ld+json">{{"@type":"BreadcrumbList"}}</script>
        <script type="application/ld+json">{script}</script>
        <div class="job-info">
          <div class="job-info__heading"><h1>Mobile Engineer</h1>
            <div class="job-info__company">Example Inc</div>
            <div class="job-info__country">{location}</div></div>
          <div class="job-info__relocation-packages">Visa services</div>
          <div class="job-info__description-item"><h2>Position</h2><p>{description}</p></div>
          <div class="job-info__description-item"><h2>Additional details</h2>{salary}</div>
          <a href="/international-jobs?query=dart">Dart</a>
        </div>
        {'<div class="archive-vacancy">Archive vacancy</div>' if archived else ''}
        <aside>Newsletter, salary estimates, recommended Flutter jobs</aside>'''


def response(html='', status=200):
    result = Mock(status_code=status, text=html)
    if status >= 400:
        http_response = requests.Response()
        http_response.status_code = status
        result.raise_for_status.side_effect = requests.HTTPError(response=http_response)
    return result


class RelocateMeHttpTests(unittest.TestCase):
    def setUp(self):
        self.api = PythonRequestsModuleRelocateMeApi()
        get_patch = patch('src.job_sources.relocate_me.python_requests_api.requests.get')
        self.get = get_patch.start()
        self.addCleanup(get_patch.stop)

    def test_listing_discovery_is_unfiltered_and_follows_explicit_pagination(self):
        self.get.return_value = response(listing_html((PROMO, PATH), next_page=2, featured=True))
        self.assertEqual(self.api.get_jobs_page(1), {'urls': [URL], 'next_page': 2})
        self.get.assert_called_once_with('https://relocate.me/international-jobs', params={'page': 1},
                                         headers=self.api.HEADERS, timeout=30)
        self.assertEqual(self.api.HEADERS['User-Agent'], 'Mozilla/5.0')

    def test_duplicates_are_collapsed_and_sponsored_actual_jobs_are_retained(self):
        self.get.return_value = response(listing_html((PATH, PATH), featured=True))
        self.assertEqual(self.api.get_jobs_page(1)['urls'], [URL])

    def test_final_page_ignores_disabled_next_link(self):
        html = listing_html() + '<a class="disabled" aria-label="Next page" href="#">Next</a>'
        self.get.return_value = response(html)
        self.assertEqual(self.api.get_jobs_page(2), {'urls': [URL], 'next_page': None})

    def test_zero_results_do_not_collect_recommendations(self):
        self.get.return_value = response(listing_html((PATH,), count=0))
        self.assertEqual(self.api.get_jobs_page(1), {'urls': [], 'next_page': None})

    def test_results_heading_accepts_live_whitespace_and_singular_job(self):
        for heading in ('<h1><span>1</span>  job available</h1>',
                        '<h1><span>24</span> \n jobs available</h1>'):
            self.get.return_value = response(listing_html().replace('<h1>1 jobs available</h1>', heading))
            self.assertEqual(self.api.get_jobs_page(1)['urls'], [URL])

    def test_detail_url_validation_prevents_requests_to_other_hosts(self):
        with self.assertRaises(RelocateMeFetchError):
            self.api.get_job_posting('https://evil.example' + PATH)
        self.get.assert_not_called()

    def test_layout_changes_and_challenge_pages_fail_explicitly(self):
        for html in ('Please verify you are human', '<h1>24 jobs available</h1>',
                     '<h1>24 jobs available</h1><div class="jobs-list"></div>'):
            self.get.return_value = response(html)
            with self.assertRaisesRegex(RelocateMeFetchError, 'invalid listing data'):
                self.api.get_jobs_page(1)

    def test_invalid_listing_urls_and_pagination_do_not_get_followed(self):
        for path in ('https://evil.example/jobs-12345', '/invalid',
                     'https://relocate.me@evil.example' + PATH):
            self.get.return_value = response(listing_html((path,)))
            with self.assertRaises(RelocateMeFetchError):
                self.api.get_jobs_page(1)
        for target in ('/international-jobs?page=1', '/international-jobs?page=3',
                       'https://evil.example/international-jobs?page=2', '/international-jobs?page=nan'):
            html = listing_html() + f'<a aria-label="Next page" href="{target}">Next</a>'
            self.get.return_value = response(html)
            with self.assertRaises(RelocateMeFetchError):
                self.api.get_jobs_page(1)

    def test_full_description_and_literal_control_characters_in_schema(self):
        self.get.return_value = response(detail_html(malformed=True))
        data = self.api.get_job_posting(URL)
        self.assertEqual(data['title'], 'Mobile Engineer')
        self.assertEqual(data['id'], '12345')
        self.assertEqual(data['date_posted'], '2026-10-12')
        self.assertIn('Build FLUTTER apps & platforms.', data['description'])
        for text in ('Company: Example Inc', 'Location: Tokyo, Japan', 'Relocation: Visa services', 'Tags: Dart'):
            self.assertIn(text, data['description'])
        self.assertNotIn('Newsletter', data['description'])
        self.assertNotIn('recommended', data['description'])
        self.assertEqual(data['salary'], '-')
        self.assertIsNone(data['remote'])

    def test_graph_and_array_job_metadata(self):
        for wrapper in (lambda schema: {'@graph': [schema]}, lambda schema: [schema]):
            self.get.return_value = response(detail_html(schema_wrapper=wrapper))
            self.assertEqual(self.api.get_job_posting(URL)['id'], '12345')

    def test_remote_requires_explicit_location_or_schema_marker(self):
        for kwargs in ({'location': 'Tokyo, Japan or Remote'},
                       {'schema_overrides': {'jobLocationType': 'TELECOMMUTE'}}):
            self.get.return_value = response(detail_html(**kwargs))
            self.assertTrue(self.api.get_job_posting(URL)['remote'])
        self.get.return_value = response(detail_html(description='Remote collaboration with teams and relocation support'))
        self.assertIsNone(self.api.get_job_posting(URL)['remote'])

    def test_salary_text_is_preserved_without_copying_rest_of_section(self):
        self.get.return_value = response(detail_html(salary='<p>Annual salary: <b>8.5 to 12.5 million JPY</b></p><p>Insurance included.</p>'))
        self.assertEqual(self.api.get_job_posting(URL)['salary'], 'Annual salary: 8.5 to 12.5 million JPY')

    def test_structured_salary_units_are_preserved_and_not_guessed(self):
        for salary, expected in (
            ({'currency': 'EUR', 'value': {'minValue': 70000, 'maxValue': 90000, 'unitText': 'YEAR'}}, 'EUR 70,000 - 90,000 / year'),
            ({'value': {'value': 100000}}, '100,000'),
            ({'value': {'maxValue': 90000}}, 'up to 90,000'),
            ({'value': {'minValue': 70000}}, 'from 70,000'),
        ):
            self.get.return_value = response(detail_html(schema_overrides={'baseSalary': salary}))
            self.assertEqual(self.api.get_job_posting(URL)['salary'], expected)

    def test_archived_removed_and_expired_jobs_are_skipped(self):
        for result in (response(detail_html(archived=True)), response(status=404), response(status=410),
                       response(detail_html(schema_overrides={'validThrough': '2020-01-01'}))):
            self.get.return_value = result
            self.assertIsNone(self.api.get_job_posting(URL))

    def test_missing_dates_wrong_id_and_invalid_metadata_fail(self):
        for overrides in ({'datePosted': None}, {'datePosted': 'bad'}, {'identifier': {'value': '999'}},
                          {'baseSalary': {'value': {'value': True}}}):
            self.get.return_value = response(detail_html(schema_overrides=overrides))
            with self.assertRaisesRegex(RelocateMeFetchError, 'invalid job data'):
                self.api.get_job_posting(URL)
        self.get.return_value = response('<h1>Not a vacancy</h1>')
        with self.assertRaises(RelocateMeFetchError):
            self.api.get_job_posting(URL)

    def test_http_and_network_failures_are_reported_for_listings_and_details(self):
        for method, arg in ((self.api.get_jobs_page, 1), (self.api.get_job_posting, URL)):
            for status in (403, 429, 500):
                self.get.side_effect = None
                self.get.return_value = response(status=status)
                with self.assertRaisesRegex(RelocateMeFetchError, f'HTTP {status}'):
                    method(arg)
            self.get.side_effect = requests.Timeout('contains request information')
            with self.assertRaisesRegex(RelocateMeFetchError, 'network connectivity'):
                method(arg)


class RelocateMeSourceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.api = Mock(spec=RelocateMeApi)
        self.api.get_jobs_page.return_value = {'urls': [URL], 'next_page': None}
        self.api.get_job_posting.return_value = posting()

    def source(self, filters=None):
        return RelocateMeJobsSource(RelocateMeJobsSourceConfig(filters or SearchFilter(keywords=['flutter'])), self.api)

    async def test_job_fields_and_stable_identity(self):
        job = (await self.source().fetch(CUTOFF))[0]
        self.assertEqual(job.title, 'Mobile Engineer')
        self.assertEqual(job.description, 'Build Flutter apps.')
        self.assertEqual(job.url, URL)
        self.assertEqual(job.salary, '-')
        self.assertIsNone(job.is_remote_allowed)
        self.assertEqual(job.fingerprint, hashlib.sha256(b'relocate.me:12345').hexdigest())

    async def test_full_text_filters_exclusions_wildcard_and_empty_keywords(self):
        for filters, count in ((SearchFilter(keywords=['FLUTTER']), 1),
                               (SearchFilter(keywords=['mobile']), 1),
                               (SearchFilter(keywords=['python']), 0),
                               (SearchFilter(keywords=['*']), 1),
                               (SearchFilter(keywords=['*'], exclude_keywords=['FLUTTER']), 0),
                               (SearchFilter(keywords=[]), 0)):
            with self.subTest(filters=filters):
                self.assertEqual(len(await self.source(filters).fetch(CUTOFF)), count)
        self.api.reset_mock()
        await self.source(SearchFilter(keywords=[])).fetch(CUTOFF)
        self.api.get_jobs_page.assert_not_called()

    async def test_all_pages_are_visited_after_old_or_duplicate_jobs(self):
        self.api.get_jobs_page.side_effect = [
            {'urls': [URL], 'next_page': 2},
            {'urls': [URL, OTHER_URL], 'next_page': None},
        ]
        self.api.get_job_posting.side_effect = [posting(date_posted='2026-09-01'), posting(id='67890')]
        jobs = await self.source().fetch(CUTOFF)
        self.assertEqual([job.url for job in jobs], [OTHER_URL])
        self.assertEqual(self.api.get_job_posting.call_count, 2)
        self.assertEqual([call.args for call in self.api.get_jobs_page.call_args_list], [(1,), (2,)])

    async def test_date_only_keeps_cutoff_day_and_timestamps_use_exact_cutoff(self):
        for date, expected in (('2026-10-11', 0), ('2026-10-12', 1),
                               ('2026-10-12T09:59:59Z', 0), ('2026-10-12T10:00:00Z', 1),
                               ('2026-10-12T13:00:00+03:00', 1)):
            self.api.get_job_posting.return_value = posting(date_posted=date)
            self.assertEqual(len(await self.source().fetch(CUTOFF)), expected)
        self.api.get_job_posting.return_value = posting()
        self.assertEqual(len(await self.source().fetch(CUTOFF.replace(tzinfo=None))), 1)
        self.assertEqual(len(await self.source().fetch(CUTOFF.astimezone(timezone(timedelta(hours=3))))), 1)

    async def test_missing_details_and_empty_results_are_skipped(self):
        self.api.get_job_posting.return_value = None
        self.assertEqual(await self.source().fetch(CUTOFF), [])
        self.api.get_jobs_page.return_value = {'urls': [], 'next_page': None}
        self.assertEqual(await self.source().fetch(CUTOFF), [])

    async def test_bad_provider_responses_do_not_silently_return_no_jobs(self):
        for page in ({}, {'urls': 'wrong', 'next_page': None}, {'urls': [URL], 'next_page': 1},
                     {'urls': [URL], 'next_page': True}, {'urls': ['https://evil.example/job-12345'], 'next_page': None}):
            self.api.get_jobs_page.return_value = page
            with self.assertRaises(RelocateMeFetchError):
                await self.source().fetch(CUTOFF)
        self.api.get_jobs_page.return_value = {'urls': [URL], 'next_page': None}
        for data in ({}, posting(id='67890'), posting(remote='yes'), posting(date_posted='invalid')):
            self.api.get_job_posting.return_value = data
            with self.assertRaises(RelocateMeFetchError):
                await self.source().fetch(CUTOFF)


if __name__ == '__main__':
    unittest.main()
