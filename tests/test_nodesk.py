import hashlib
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs

import requests

from src.job_sources.nodesk.nodesk import (
    NoDeskFetchError,
    NoDeskJobsSource,
    NoDeskJobsSourceConfig,
)
from src.job_sources.nodesk.python_requests_api import PythonRequestsModuleNoDeskApi
from src.search_filter import SearchFilter


CUTOFF = datetime(2026, 10, 12, 10, tzinfo=timezone.utc)


def vacancy(**overrides):
    record = {
        'objectID': 'job-1',
        'title': 'Mobile Engineer',
        'datePublished': '2026-10-12T10:00:00',
        'permalink': '/remote-jobs/example-mobile-engineer/',
        'company': {'name': 'Example Inc'},
        'applicantLocations': [{'name': 'Europe'}],
        'employmentTypes': [{'name': 'Full-Time'}],
        'keywords': [{'name': 'Dart'}],
        'baseSalary': '$30K – $48K',
    }
    record.update(overrides)
    return record


def listing_response(records, page=0, pages=1, total=None):
    response = MagicMock(status_code=200)
    response.json.return_value = {'results': [{
        'hits': records, 'nbPages': pages, 'page': page,
        'nbHits': total if total is not None else len(records) + 90 * max(pages - 1, 0),
    }]}
    return response


def posting(**overrides):
    data = {
        '@context': 'https://schema.org/',
        '@type': 'JobPosting',
        'title': 'Mobile Engineer',
        'description': '<p>Build FLUTTER apps &amp; platforms.</p><ul><li>Own releases.</li></ul>',
        'datePosted': '2026-10-12',
        'jobLocationType': 'TELECOMMUTE',
        'baseSalary': {
            'currency': 'USD',
            'value': {'minValue': 30000, 'maxValue': 48000, 'unitText': 'YEAR'},
        },
    }
    data.update(overrides)
    return data


def detail_response(data=None):
    response = MagicMock(status_code=200)
    response.text = '<script type="application/ld+json">' + json.dumps(
        posting() if data is None else data,
    ) + '</script>'
    return response


class NoDeskTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.source = self.source_with(SearchFilter(keywords=['flutter']))
        self.listings = [listing_response([vacancy()])]
        self.details = detail_response()
        self.detail_responses = {}
        post_patch = patch('src.job_sources.nodesk.python_requests_api.requests.post', side_effect=self.search_request)
        get_patch = patch('src.job_sources.nodesk.python_requests_api.requests.get', side_effect=self.detail_request)
        self.search = post_patch.start()
        self.get = get_patch.start()
        self.addCleanup(post_patch.stop)
        self.addCleanup(get_patch.stop)

    def source_with(self, filters):
        self.api = PythonRequestsModuleNoDeskApi()
        return NoDeskJobsSource(NoDeskJobsSourceConfig(filters), self.api)

    def search_request(self, *args, **kwargs):
        return self.listings.pop(0)

    def detail_request(self, url, **kwargs):
        return self.detail_responses.get(url, self.details)

    def query_params(self):
        return [parse_qs(call.kwargs['json']['requests'][0]['params'], keep_blank_values=True)
                for call in self.search.call_args_list]

    async def test_public_raw_json_request_and_full_description_fields(self):
        job = (await self.source.fetch(CUTOFF))[0]
        call = self.search.call_args
        self.assertEqual(call.args, (self.api.API_URL,))
        self.assertEqual(call.kwargs['params'], self.api.API_PARAMS)
        self.assertEqual(call.kwargs['headers']['Origin'], 'https://nodesk.co')
        self.assertEqual(call.kwargs['timeout'], 30)
        self.assertEqual(call.kwargs['json']['requests'][0]['indexName'], 'jobPosts')
        params = self.query_params()[0]
        self.assertEqual(params['query'], [''])
        self.assertEqual(params['filters'], ['searchFilter:remote-jobs'])
        self.assertEqual(params['hitsPerPage'], ['90'])
        self.assertEqual(job.title, 'Mobile Engineer')
        self.assertTrue(job.description.startswith('Build FLUTTER apps & platforms.\nOwn releases.'))
        self.assertIn('Company: Example Inc', job.description)
        self.assertIn('Location: Europe', job.description)
        self.assertIn('Contract: Full-Time', job.description)
        self.assertIn('Tags: Dart', job.description)
        self.assertEqual(job.salary, 'USD 30,000 - 48,000 / year')
        self.assertTrue(job.is_remote_allowed)
        self.assertEqual(job.url, 'https://nodesk.co/remote-jobs/example-mobile-engineer/')
        self.assertEqual(job.fingerprint, hashlib.sha256(b'nodesk.co:job-1').hexdigest())
        self.get.assert_called_once_with(job.url, headers=self.api.HEADERS, timeout=30)

    async def test_all_pages_are_fetched_despite_old_first_page(self):
        self.listings = [
            listing_response([vacancy(datePublished='2026-09-01T00:00:00')], pages=3),
            listing_response([vacancy()], page=1, pages=3),
            listing_response([vacancy(objectID='second', permalink='/remote-jobs/second/')], page=2, pages=3),
        ]
        self.assertEqual(len(await self.source.fetch(CUTOFF)), 2)
        self.assertEqual([params['page'] for params in self.query_params()], [['0'], ['1'], ['2']])
        self.assertEqual(self.get.call_count, 2)

    async def test_date_cutoff_is_inclusive_and_normalizes_timezones(self):
        records = [
            vacancy(objectID='old', datePublished='2026-10-12T09:59:59Z'),
            vacancy(objectID='boundary', datePublished='2026-10-12T13:00:00+03:00'),
            vacancy(objectID='new', datePublished='2026-10-12T10:00:01'),
        ]
        for cutoff in (CUTOFF, CUTOFF.replace(tzinfo=None),
                       CUTOFF.astimezone(timezone(timedelta(hours=3)))):
            with self.subTest(cutoff=cutoff):
                self.listings = [listing_response(records)]
                self.get.reset_mock()
                self.assertEqual(len(await self.source.fetch(cutoff)), 2)
                self.assertEqual(self.get.call_count, 2)

    async def test_duplicates_across_pages_fetch_details_once_even_when_rejected(self):
        self.listings = [listing_response([vacancy()], pages=2),
                         listing_response([vacancy(permalink='/remote-jobs/renamed/')], page=1, pages=2)]
        self.details = detail_response(posting(description='Unpaid Flutter internship'))
        source = self.source_with(SearchFilter(keywords=['flutter', 'dart'], exclude_keywords=['UNPAID']))
        self.assertEqual(await source.fetch(CUTOFF), [])
        self.get.assert_called_once()

    async def test_description_only_matches_and_exclusions_override_title_and_wildcard(self):
        self.assertEqual(len(await self.source.fetch(CUTOFF)), 1)
        for keywords in (['flutter'], ['*']):
            with self.subTest(keywords=keywords):
                self.listings = [listing_response([vacancy(title='Flutter Engineer')])]
                self.details = detail_response(posting(description='<p>UNPAID internship</p>'))
                source = self.source_with(SearchFilter(keywords=keywords, exclude_keywords=['unpaid']))
                self.assertEqual(await source.fetch(CUTOFF), [])

    async def test_unrelated_details_are_filtered_locally(self):
        self.details = detail_response(posting(description='Build native iOS apps'))
        self.assertEqual(await self.source.fetch(CUTOFF), [])

    async def test_empty_keywords_make_no_requests(self):
        self.assertEqual(await self.source_with(SearchFilter(keywords=[])).fetch(CUTOFF), [])
        self.search.assert_not_called()
        self.get.assert_not_called()

    async def test_empty_index_with_zero_pages_returns_no_jobs(self):
        self.listings = [listing_response([], pages=0)]
        self.assertEqual(await self.source.fetch(CUTOFF), [])
        self.get.assert_not_called()

    async def test_advertised_nodesk_vacancy_gets_full_details(self):
        self.listings = [listing_response([vacancy(is_ad=True)])]
        self.assertEqual(len(await self.source.fetch(CUTOFF)), 1)
        self.get.assert_called_once()

    async def test_external_advertised_vacancy_is_included_with_copy_and_badges(self):
        self.listings = [listing_response([vacancy(
            is_ad=True, title='Remote Customer Support',
            permalink='https://remotetalentcloud.com/apply/?ref=nodesk',
            description='<p>Support customers using Flutter apps.</p>',
            badges=[{'name': '$20/hr+'}, {'name': 'Full-Time/Part-Time'}],
            baseSalary=False, keywords=[], applicantLocations=[], employmentTypes=[],
        )])]
        job = (await self.source.fetch(CUTOFF))[0]
        self.assertEqual(job.url, 'https://remotetalentcloud.com/apply/?ref=nodesk')
        self.assertIn('Support customers using Flutter apps.', job.description)
        self.assertIn('$20/hr+', job.description)
        self.assertEqual(job.salary, '-')
        self.get.assert_not_called()

    async def test_external_ad_cta_fallback_and_exclusions(self):
        ad = vacancy(is_ad=True, permalink=None, ctaUrl='https://example.com/apply',
                     description='Unpaid Flutter internship')
        self.listings = [listing_response([ad])]
        self.assertEqual(len(await self.source.fetch(CUTOFF)), 1)
        self.listings = [listing_response([ad])]
        source = self.source_with(SearchFilter(exclude_keywords=['unpaid']))
        self.assertEqual(await source.fetch(CUTOFF), [])
        self.get.assert_not_called()

    async def test_removed_detail_is_skipped_and_search_continues(self):
        for status in (404, 410):
            with self.subTest(status=status):
                self.listings = [listing_response([vacancy(), vacancy(objectID='second', permalink='/remote-jobs/second/')])]
                missing = MagicMock(status_code=status)
                self.detail_responses['https://nodesk.co/remote-jobs/example-mobile-engineer/'] = missing
                jobs = await self.source.fetch(CUTOFF)
                self.assertEqual(len(jobs), 1)
                self.assertTrue(jobs[0].url.endswith('/second/'))
                missing.raise_for_status.assert_not_called()

    async def test_json_ld_arrays_graphs_and_type_lists(self):
        for data in ([{'@type': 'Organization'}, posting()],
                     {'@graph': [{'@type': 'Organization'}, posting()]},
                     posting(**{'@type': ['Thing', 'JobPosting']})):
            with self.subTest(data=data):
                self.listings = [listing_response([vacancy()])]
                self.details = detail_response(data)
                self.details.text = '<script type="application/ld+json">bad</script>' + self.details.text
                self.assertEqual(len(await self.source.fetch(CUTOFF)), 1)

    async def test_salary_fallback_bounds_and_period(self):
        salaries = [
            (None, '$30K – $48K'),
            ({'value': {'minValue': 20, 'unitText': 'HOUR'}, 'currency': 'USD'}, 'USD from 20 / hour'),
            ({'value': {'maxValue': 2133.5, 'unitText': 'MONTH'}}, 'up to 2,133.5 / month'),
            ({'value': {'value': 0}, 'currency': 'GBP'}, 'GBP 0'),
            ({'value': {}}, '-'),
        ]
        for salary, expected in salaries:
            with self.subTest(salary=salary):
                self.listings = [listing_response([vacancy()])]
                self.details = detail_response(posting(baseSalary=salary))
                self.assertEqual((await self.source.fetch(CUTOFF))[0].salary, expected)

    async def test_invalid_payload_pagination_and_records_fail_clearly(self):
        payloads = [[], {}, {'results': []}, {'results': ['bad']}]
        for record in (vacancy(datePublished='bad'), vacancy(objectID=None),
                       vacancy(permalink='javascript:alert(1)'), vacancy(permalink='/contact/'),
                       vacancy(company=['bad']), vacancy(applicantLocations=[{'name': 123}]),
                       vacancy(title=[])):
            payloads.append(listing_response([record]).json.return_value)
        for field, value in (('nbPages', True), ('nbPages', -1), ('nbPages', 101),
                             ('nbHits', '1'), ('nbHits', 1000), ('page', True), ('page', 1),
                             ('hits', ['bad']), ('hits', [])):
            payload = listing_response([vacancy()]).json.return_value
            payload['results'][0][field] = value
            payloads.append(payload)
        for payload in payloads:
            with self.subTest(payload=payload):
                result = MagicMock(status_code=200)
                result.json.return_value = payload
                self.listings = [result]
                with self.assertRaisesRegex(NoDeskFetchError, 'invalid job data'):
                    await self.source.fetch(CUTOFF)

    async def test_invalid_search_json_fails_clearly(self):
        self.listings[0].json.side_effect = ValueError('invalid JSON')
        with self.assertRaisesRegex(NoDeskFetchError, 'invalid job data'):
            await self.source.fetch(CUTOFF)

    async def test_missing_or_invalid_full_description_fails_clearly(self):
        for data in ({'@type': 'Organization'}, posting(description=None),
                     posting(description=[]), posting(description=''), posting(title=[]),
                     posting(baseSalary={'value': {'minValue': True}}),
                     posting(baseSalary={'value': {'minValue': float('inf')}})):
            with self.subTest(data=data):
                self.listings = [listing_response([vacancy()])]
                self.details = detail_response(data)
                with self.assertRaisesRegex(NoDeskFetchError, 'invalid job data'):
                    await self.source.fetch(CUTOFF)
        self.listings = [listing_response([vacancy()])]
        self.details.text = '<html>Access denied</html>'
        with self.assertRaisesRegex(NoDeskFetchError, 'invalid job data'):
            await self.source.fetch(CUTOFF)

    async def test_http_failures_on_search_and_details_are_reported(self):
        for endpoint in ('search', 'detail'):
            for status in (403, 429, 500):
                with self.subTest(endpoint=endpoint, status=status):
                    self.listings = [listing_response([vacancy()])]
                    self.details = detail_response()
                    response = requests.Response()
                    response.status_code = status
                    failed = self.listings[0] if endpoint == 'search' else self.details
                    failed.raise_for_status.side_effect = requests.HTTPError(response=response)
                    with self.assertRaisesRegex(NoDeskFetchError, f'HTTP {status}'):
                        await self.source.fetch(CUTOFF)

    async def test_network_failures_on_search_and_details_are_reported(self):
        self.search.side_effect = requests.Timeout('search timeout')
        with self.assertRaisesRegex(NoDeskFetchError, 'network connectivity'):
            await self.source.fetch(CUTOFF)
        self.search.side_effect = self.search_request
        self.get.side_effect = requests.Timeout('detail timeout')
        with self.assertRaisesRegex(NoDeskFetchError, 'network connectivity'):
            await self.source.fetch(CUTOFF)


if __name__ == '__main__':
    unittest.main()
