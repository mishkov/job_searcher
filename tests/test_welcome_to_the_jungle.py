import hashlib
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from uuid import UUID

import requests

from src.job_sources.welcome_to_the_jungle.welcome_to_the_jungle import (
    WelcomeToTheJungleFetchError,
    WelcomeToTheJungleJobsSource,
    WelcomeToTheJungleJobsSourceConfig,
)
from src.search_filter import SearchFilter


CUTOFF = datetime(2026, 10, 12, 10, tzinfo=timezone.utc)


def vacancy(**overrides):
    record = {
        'name': 'Flutter Engineer (Contract)',
        'status': 'published',
        'reference': 'e942d4c9-1694-49c7-84a2-e9b1aeebd10b',
        'slug': 'flutter-engineer-contract_london_ozyx3a5e',
        'published_at': '2026-10-12T10:00:00Z',
        'updated_at': '2026-10-12T12:00:00Z',
        'remote': 'partial',
        'organization': {
            'name': 'AND Digital',
            'slug': 'internal-slug',
            'website_organization': {'slug': 'and-digital'},
        },
        'offices': [{'city': 'London', 'country_code': 'GB'}],
        'contract_type': 'freelance',
        'company_summary': '<p>Build apps &amp; platforms.</p>',
        'salary_min': 90000,
        'salary_max': 120000,
        'salary_currency': 'GBP',
        'salary_period': 'yearly',
    }
    record.update(overrides)
    return record


def response(records, page=1, page_count=1):
    result = MagicMock()
    result.status_code = 200
    result.json.return_value = {
        'data': records,
        'metadata': {'page': page, 'page_count': page_count},
    }
    return result


def detail_response(**fields):
    result = MagicMock()
    result.status_code = 200
    result.json.return_value = {'job': fields}
    return result


class WelcomeToTheJungleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.source = self.source_with(SearchFilter(keywords=['flutter']))
        self.response = response([vacancy()])
        self.detail_response = detail_response()
        self.page_responses = None
        self.detail_responses = {}
        self.request_patch = patch(
            'src.job_sources.welcome_to_the_jungle.welcome_to_the_jungle.requests.get',
            side_effect=self.api_request,
        )
        self.request = self.request_patch.start()
        self.addCleanup(self.request_patch.stop)

    def source_with(self, filters):
        return WelcomeToTheJungleJobsSource(WelcomeToTheJungleJobsSourceConfig(filters))

    def api_request(self, url, **kwargs):
        if url == self.source.API_URL:
            return self.page_responses.pop(0) if self.page_responses is not None else self.response
        return self.detail_responses.get(url, self.detail_response)

    def listing_calls(self):
        return [call for call in self.request.call_args_list if call.args[0] == self.source.API_URL]

    def detail_calls(self):
        return [call for call in self.request.call_args_list if call.args[0] != self.source.API_URL]

    async def test_public_query_and_api_fields(self):
        jobs = await self.source.fetch(CUTOFF)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(self.listing_calls()[0].args, (self.source.API_URL,))
        args = self.listing_calls()[0].kwargs
        self.assertEqual(args['timeout'], 15)
        self.assertEqual(args['params']['job_title'], 'flutter')
        self.assertEqual(args['params']['include_empty_salary'], 'true')
        self.assertEqual(UUID(args['params']['query_id']).version, 4)
        job = jobs[0]
        self.assertEqual(job.title, 'Flutter Engineer (Contract)')
        self.assertIn('Company: AND Digital', job.description)
        self.assertIn('Location: London, GB', job.description)
        self.assertIn('Remote: Hybrid', job.description)
        self.assertIn('Build apps & platforms.', job.description)
        self.assertEqual(job.salary, 'GBP 90,000 - 120,000 / yearly')
        self.assertTrue(job.is_remote_allowed)
        self.assertEqual(job.url, 'https://www.welcometothejungle.com/en/companies/and-digital/jobs/flutter-engineer-contract_london_ozyx3a5e')
        self.assertEqual(job.fingerprint, hashlib.sha256(
            b'welcometothejungle.com:e942d4c9-1694-49c7-84a2-e9b1aeebd10b'
        ).hexdigest())

    async def test_old_first_page_does_not_hide_new_jobs_on_later_pages(self):
        self.page_responses = [
            response([vacancy(published_at='2026-09-01T00:00:00Z')], page_count=3),
            response([vacancy()], page=2, page_count=3),
            response([vacancy(reference='second')], page=3, page_count=3),
        ]
        self.assertEqual(len(await self.source.fetch(CUTOFF)), 2)
        params = [call.kwargs['params'] for call in self.listing_calls()]
        self.assertEqual([p['page'] for p in params], [1, 2, 3])
        self.assertEqual(len({p['query_id'] for p in params}), 1)

    async def test_publication_cutoff_timezone_boundary_and_unpublished_jobs(self):
        self.response.json.return_value['data'] = [
            vacancy(reference='old', published_at='2026-10-12T09:59:59Z'),
            vacancy(reference='boundary', published_at='2026-10-12T13:00:00+03:00'),
            vacancy(reference='new', published_at='2026-10-12T10:00:01Z'),
            vacancy(reference='draft', status='draft'),
        ]
        self.assertEqual(len(await self.source.fetch(CUTOFF.replace(tzinfo=None))), 2)

    async def test_overlapping_keywords_and_pages_deduplicate_by_reference(self):
        source = self.source_with(SearchFilter(keywords=['flutter', 'dart', 'flutter']))
        self.page_responses = [
            response([vacancy()], page_count=2),
            response([vacancy(slug='renamed-job')], page=2, page_count=2),
            response([vacancy()]),
        ]
        self.assertEqual(len(await source.fetch(CUTOFF)), 1)
        params = [call.kwargs['params'] for call in self.listing_calls()]
        self.assertEqual([p['job_title'] for p in params], ['flutter', 'flutter', 'dart'])
        self.assertNotEqual(params[0]['query_id'], params[2]['query_id'])
        self.assertEqual(len(self.detail_calls()), 1)

    async def test_new_fetch_generates_new_query_id(self):
        await self.source.fetch(CUTOFF)
        await self.source.fetch(CUTOFF)
        ids = [call.kwargs['params']['query_id'] for call in self.listing_calls()]
        self.assertNotEqual(ids[0], ids[1])

    async def test_wildcard_omits_title_and_empty_keywords_make_no_requests(self):
        await self.source_with(SearchFilter(keywords=['flutter', '*'])).fetch(CUTOFF)
        self.assertNotIn('job_title', self.listing_calls()[0].kwargs['params'])
        self.request.reset_mock()
        self.assertEqual(await self.source_with(SearchFilter(keywords=[])).fetch(CUTOFF), [])
        self.request.assert_not_called()

    async def test_local_keywords_and_exclusions_override_fuzzy_api_results(self):
        self.response.json.return_value['data'] = [
            vacancy(reference='title', company_summary=None),
            vacancy(reference='summary', name='Mobile Engineer', company_summary='FLUTTER apps'),
            vacancy(reference='unrelated', name='Fleet Manager', company_summary=None),
            vacancy(reference='excluded', company_summary='Unpaid Flutter internship'),
        ]
        source = self.source_with(SearchFilter(keywords=['FLUTTER'], exclude_keywords=['UNPAID']))
        self.assertEqual(len(await source.fetch(CUTOFF)), 2)
        source = self.source_with(SearchFilter(exclude_keywords=['UNPAID']))
        self.assertEqual(len(await source.fetch(CUTOFF)), 3)

    async def test_remote_values_and_optional_summary_metadata(self):
        self.response.json.return_value['data'] = [vacancy(
            reference=str(index), remote=remote, offices=None, office=None,
            company_summary=None, contract_type=None, salary_min=None, salary_max=None,
        ) for index, remote in enumerate(('partial', 'fulltime', 'no', 'unknown', None))]
        jobs = await self.source.fetch(CUTOFF)
        self.assertEqual([job.is_remote_allowed for job in jobs], [True, True, False, None, None])
        self.assertTrue(all(job.salary == '-' for job in jobs))

    async def test_salary_bounds_zero_and_fractional_amounts(self):
        self.response.json.return_value['data'] = [
            vacancy(reference='min', salary_min=23000, salary_max=None),
            vacancy(reference='max', salary_min=None, salary_max=2133.5, salary_period='monthly'),
            vacancy(reference='zero', salary_min=0, salary_max=0),
            vacancy(reference='bare', salary_min=38, salary_max=45, salary_currency=None, salary_period=None),
            vacancy(reference='cents', salary_min=123456.78, salary_max=123456.78),
        ]
        self.assertEqual([job.salary for job in await self.source.fetch(CUTOFF)], [
            'GBP from 23,000 / yearly', 'GBP up to 2,133.5 / monthly',
            'GBP 0 / yearly', '38 - 45', 'GBP 123,456.78 / yearly',
        ])

    async def test_office_and_organization_slug_fallback(self):
        self.response.json.return_value['data'] = [vacancy(
            organization={'name': 'Example', 'slug': 'example', 'website_organization': None},
            offices=[], office={'city': 'Paris', 'country_code': 'FR'},
        )]
        job = (await self.source.fetch(CUTOFF))[0]
        self.assertIn('/companies/example/jobs/', job.url)
        self.assertIn('Location: Paris, FR', job.description)

    async def test_empty_response_stops_even_if_metadata_has_more_pages(self):
        for count in (0, 1, 5):
            self.response = response([], page_count=count)
            self.request.reset_mock()
            self.assertEqual(await self.source.fetch(CUTOFF), [])
            self.request.assert_called_once()

    async def test_invalid_payload_dates_identity_and_pagination_fail_clearly(self):
        payloads = [[], {}, {'data': ['bad'], 'metadata': {'page': 1, 'page_count': 1}}]
        for record in (vacancy(published_at='bad'), vacancy(reference=None),
                       vacancy(organization=None), vacancy(slug=None),
                       vacancy(offices=['invalid']), vacancy(company_summary=[]),
                       vacancy(name=[]), vacancy(offices=[{'city': 123}])):
            payloads.append(response([record]).json.return_value)
        for count in (-1, True, '2'):
            payloads.append(response([vacancy()], page_count=count).json.return_value)
        payloads.append(response([vacancy()], page=2, page_count=2).json.return_value)
        for payload in payloads:
            with self.subTest(payload=payload):
                self.response.json.return_value = payload
                with self.assertRaisesRegex(WelcomeToTheJungleFetchError, 'invalid job data'):
                    await self.source.fetch(CUTOFF)

    async def test_invalid_json_fails_clearly(self):
        self.response.json.side_effect = ValueError('not JSON')
        with self.assertRaisesRegex(WelcomeToTheJungleFetchError, 'invalid job data'):
            await self.source.fetch(CUTOFF)

    async def test_http_and_network_failures(self):
        for status in (403, 429, 500):
            with self.subTest(status=status):
                failed = requests.Response()
                failed.status_code = status
                self.response.raise_for_status.side_effect = requests.HTTPError(response=failed)
                with self.assertRaisesRegex(WelcomeToTheJungleFetchError, f'HTTP {status}'):
                    await self.source.fetch(CUTOFF)
        self.request.side_effect = requests.Timeout('timed out')
        with self.assertRaisesRegex(WelcomeToTheJungleFetchError, 'network connectivity'):
            await self.source.fetch(CUTOFF)

    async def test_full_detail_sections_and_listing_identity_are_preserved(self):
        self.detail_response = detail_response(
            description='<p>Build Flutter apps &amp; platforms.</p><ul><li>Own releases.</li></ul>',
            profile='<p>Experience with Dart.</p>',
            recruitment_process='<p>Interview then offer.</p>',
            reference=None, status='published',
        )
        job = (await self.source.fetch(CUTOFF))[0]
        self.assertTrue(job.description.startswith('Job description:\nBuild Flutter apps & platforms.\nOwn releases.'))
        self.assertIn('Preferred experience:\nExperience with Dart.', job.description)
        self.assertIn('Recruitment process:\nInterview then offer.', job.description)
        self.assertIn('Company: AND Digital', job.description)
        self.assertEqual(job.fingerprint, hashlib.sha256(
            b'welcometothejungle.com:e942d4c9-1694-49c7-84a2-e9b1aeebd10b'
        ).hexdigest())
        self.assertEqual(job.salary, 'GBP 90,000 - 120,000 / yearly')
        self.assertEqual(job.url, 'https://www.welcometothejungle.com/en/companies/and-digital/jobs/flutter-engineer-contract_london_ozyx3a5e')
        self.assertEqual(self.detail_calls()[0].args, (
            'https://api.welcometothejungle.com/api/v1/organizations/and-digital/jobs/flutter-engineer-contract_london_ozyx3a5e',
        ))
        self.assertEqual(self.detail_calls()[0].kwargs, {'params': None, 'timeout': 15})

    async def test_keyword_only_in_full_description_or_profile_is_included(self):
        self.response.json.return_value['data'] = [vacancy(name='Mobile Engineer', company_summary=None)]
        for field in ('description', 'profile', 'recruitment_process'):
            with self.subTest(field=field):
                self.detail_response = detail_response(**{field: '<p>FLUTTER development</p>'})
                self.assertEqual(len(await self.source.fetch(CUTOFF)), 1)

    async def test_exclusions_in_full_details_override_title_and_wildcard(self):
        for keywords in (['flutter'], ['*']):
            for field in ('description', 'profile', 'recruitment_process'):
                with self.subTest(keywords=keywords, field=field):
                    self.detail_response = detail_response(**{field: '<p>UNPAID internship</p>'})
                    source = self.source_with(SearchFilter(keywords=keywords, exclude_keywords=['unpaid']))
                    self.assertEqual(await source.fetch(CUTOFF), [])

    async def test_old_and_unpublished_listings_make_no_detail_requests(self):
        self.response.json.return_value['data'] = [
            vacancy(published_at='2026-10-12T09:59:59Z'), vacancy(status='archived'),
        ]
        self.assertEqual(await self.source.fetch(CUTOFF), [])
        self.assertEqual(self.detail_calls(), [])

    async def test_rejected_duplicates_fetch_details_only_once(self):
        self.response.json.return_value['data'] = [vacancy(), vacancy(slug='renamed-job')]
        self.detail_response = detail_response(description='Unpaid Flutter internship')
        source = self.source_with(SearchFilter(keywords=['flutter', 'dart'], exclude_keywords=['unpaid']))
        self.assertEqual(await source.fetch(CUTOFF), [])
        self.assertEqual(len(self.detail_calls()), 1)

    async def test_removed_job_is_skipped_and_next_job_is_returned(self):
        for status in (404, 410):
            with self.subTest(status=status):
                self.response.json.return_value['data'] = [vacancy(), vacancy(reference='second', slug='other-job')]
                missing = MagicMock(status_code=status)
                self.detail_responses = {self.source.DETAIL_API_URL.format(
                    organization='and-digital', job='flutter-engineer-contract_london_ozyx3a5e',
                ): missing}
                jobs = await self.source.fetch(CUTOFF)
                self.assertEqual(len(jobs), 1)
                self.assertTrue(jobs[0].url.endswith('/other-job'))
                missing.json.assert_not_called()

    async def test_unpublished_detail_status_is_skipped(self):
        self.detail_response = detail_response(status='archived', description='Flutter job')
        self.assertEqual(await self.source.fetch(CUTOFF), [])

    async def test_missing_description_fields_fall_back_to_summary(self):
        self.detail_response = detail_response(description=None, profile='', recruitment_process='<p> </p>')
        job = (await self.source.fetch(CUTOFF))[0]
        self.assertIn('Build apps & platforms.', job.description)
        self.assertNotIn('Job description:', job.description)
        self.assertNotIn('Preferred experience:', job.description)

    async def test_invalid_detail_payloads_and_json_fail_clearly(self):
        for payload in (None, [], {}, {'job': None}, {'job': []},
                        {'job': {'description': []}}, {'job': {'profile': 123}},
                        {'job': {'recruitment_process': {}}}):
            with self.subTest(payload=payload):
                self.detail_response.json.return_value = payload
                with self.assertRaisesRegex(WelcomeToTheJungleFetchError, 'invalid job data'):
                    await self.source.fetch(CUTOFF)
        self.detail_response.json.side_effect = ValueError('invalid JSON')
        with self.assertRaisesRegex(WelcomeToTheJungleFetchError, 'invalid job data'):
            await self.source.fetch(CUTOFF)

    async def test_detail_http_and_network_errors_fail_clearly(self):
        for status in (403, 429, 500):
            with self.subTest(status=status):
                failed = requests.Response()
                failed.status_code = status
                self.detail_response.raise_for_status.side_effect = requests.HTTPError(response=failed)
                with self.assertRaisesRegex(WelcomeToTheJungleFetchError, f'HTTP {status}'):
                    await self.source.fetch(CUTOFF)

        def unavailable_detail(url, **kwargs):
            if url == self.source.API_URL:
                return self.response
            raise requests.Timeout('detail API timeout')

        self.request.side_effect = unavailable_detail
        with self.assertRaisesRegex(WelcomeToTheJungleFetchError, 'network connectivity'):
            await self.source.fetch(CUTOFF)


if __name__ == '__main__':
    unittest.main()
