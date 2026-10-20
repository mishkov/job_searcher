import hashlib
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import requests

from src.job_sources.andersen.andersen import AndersenFetchError, AndersenJobsSource, AndersenJobsSourceConfig
from src.search_filter import SearchFilter


CUTOFF = datetime(2026, 10, 20, 10, tzinfo=timezone.utc)


def vacancy(**overrides):
    # Public v5 listing shape: the full content is in the listing itself.
    record = {
        'vacancy_id': 2507608,
        'name': 'Flutter Engineer',
        'summary': [{'TYPE': 'text', 'content': 'Build apps &amp; platforms.'}],
        'responsibilities': [{'TYPE': 'list', 'content': ['Own releases.', 'Build <b>mobile</b> apps.']}],
        'requirements': [{'TYPE': 'list', 'content': ['Experience with Dart.']}],
        'niceToHave': [{'TYPE': 'list', 'content': ['']}],
        'joinUsReason': [{'TYPE': 'text', 'content': 'Health insurance.'}],
        'level': 'Middle',
        'technologies': ['Flutter'],
        'specializations': ['Mobile'],
        'isRemote': False,
        'isWorldWide': False,
        'lookingLocations': [{'countryName': 'Poland', 'countryCode': 'PL', 'cities': ['Warsaw']}],
        'notLookingLocations': [{'countryName': 'Example country', 'countryCode': 'XX'}],
        'salary': {'minSalary': 1000, 'maxSalary': 2000, 'currencyCode': 'EUR'},
    }
    record.update(overrides)
    return record


class AndersenTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.source = self.source_with(SearchFilter(keywords=['flutter']))
        self.response = MagicMock()
        self.response.json.return_value = [vacancy()]
        patcher = patch('src.job_sources.andersen.andersen.requests.get', return_value=self.response)
        self.request = patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def source_with(filters):
        return AndersenJobsSource(AndersenJobsSourceConfig(filters))

    async def test_public_v5_api_full_content_and_job_mapping(self):
        job, = await self.source.fetch(CUTOFF)
        self.request.assert_called_once_with(
            self.source.API_URL, headers={'Accept-Language': 'en', 'Api-Version': 'v5'}, timeout=30,
        )
        self.assertEqual(job.title, 'Flutter Engineer')
        for text in ('Build apps & platforms.', 'Responsibilities:\nOwn releases.', 'mobile',
                     'Requirements:\nExperience with Dart.', 'Reasons to join:\nHealth insurance.',
                     'Company: Andersen', 'Level: Middle', 'Technologies: Flutter',
                     'Specializations: Mobile', 'Location: Warsaw, Poland', 'Not hiring in: Example country'):
            self.assertIn(text, job.description)
        self.assertNotIn('<b>', job.description)
        self.assertNotIn('Nice to have:', job.description)
        self.assertEqual(job.salary, 'EUR 1,000 - 2,000')
        self.assertFalse(job.is_remote_allowed)
        self.assertEqual(job.url, 'https://people-andersenlab.com/vacancy/2507608')
        self.assertEqual(job.fingerprint, hashlib.sha256(b'people-andersenlab.com:2507608').hexdigest())

    async def test_undated_jobs_remain_eligible_and_list_is_fresh_on_each_run(self):
        first, = await self.source.fetch(CUTOFF)
        self.response.json.return_value = [vacancy(name='Renamed Flutter Engineer')]
        second, = await self.source.fetch(CUTOFF + timedelta(days=10))
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(second.title, 'Renamed Flutter Engineer')
        self.assertEqual(self.request.call_count, 2)
        self.response.json.return_value = []
        self.assertEqual(await self.source.fetch(CUTOFF), [])

    async def test_optional_opening_date_utc_boundary_date_only_and_unsorted_records(self):
        self.response.json.return_value = [
            vacancy(vacancy_id=1, openingDate='2026-10-19'),
            vacancy(vacancy_id=2, openingDate='2026-10-20T09:59:59Z'),
            vacancy(vacancy_id=3, openingDate='2026-10-20T13:00:00+03:00'),
            vacancy(vacancy_id=4, openingDate='2026-10-20'),
            vacancy(vacancy_id=5, openingDate='2026-10-20T10:00:01'),
            vacancy(vacancy_id=6),
        ]
        for cutoff in (CUTOFF, CUTOFF.replace(tzinfo=None), CUTOFF.astimezone(timezone(timedelta(hours=3)))):
            with self.subTest(cutoff=cutoff):
                jobs = await self.source.fetch(cutoff)
                self.assertEqual([job.url.rsplit('/', 1)[1] for job in jobs], ['3', '4', '5', '6'])

    async def test_full_text_keywords_and_exclusions_in_every_section(self):
        for field in ('summary', 'requirements', 'responsibilities', 'niceToHave', 'joinUsReason'):
            for kind, content in (('text', 'FLUTTER development'), ('list', ['FLUTTER development'])):
                with self.subTest(field=field, kind=kind):
                    record = vacancy(name='Engineer', technologies=[], **{field: [{'TYPE': kind, 'content': content}]})
                    self.response.json.return_value = [record]
                    self.assertEqual(len(await self.source.fetch(CUTOFF)), 1)
                    record[field] = [{'TYPE': 'text', 'content': 'Unpaid FLUTTER development'}]
                    for keywords in (['flutter'], ['*']):
                        source = self.source_with(SearchFilter(keywords, ['UNPAID']))
                        self.assertEqual(await source.fetch(CUTOFF), [])

    async def test_keyword_or_semantics_and_empty_keywords_skip_request(self):
        self.response.json.return_value = [vacancy(name='Engineer', technologies=[])]
        source = self.source_with(SearchFilter(['python', 'DART']))
        self.assertEqual(len(await source.fetch(CUTOFF)), 1)
        self.assertEqual(await self.source.fetch(CUTOFF), [])
        self.request.reset_mock()
        self.assertEqual(await self.source_with(SearchFilter([])).fetch(CUTOFF), [])
        self.request.assert_not_called()

    async def test_wildcard_and_duplicate_ids(self):
        self.response.json.return_value = [vacancy(), vacancy(name='Renamed'),
                                           vacancy(vacancy_id=2, name='Java Engineer', technologies=['Java'])]
        self.assertEqual(len(await self.source_with(SearchFilter()).fetch(CUTOFF)), 2)
        self.assertEqual(len(await self.source.fetch(CUTOFF)), 1)

    async def test_optional_metadata_remote_flags_and_worldwide_is_not_remote(self):
        self.response.json.return_value = [vacancy(
            vacancy_id=i, isRemote=remote, isWorldWide=True, salary=None, lookingLocations=None,
            notLookingLocations=None, level=None, technologies=None, specializations=None,
            niceToHave=None, responsibilities=None,
        ) for i, remote in enumerate((True, False, None), 1)]
        jobs = await self.source.fetch(CUTOFF)
        self.assertEqual([job.is_remote_allowed for job in jobs], [True, False, None])
        self.assertTrue(all(job.salary == '-' and 'Location: Worldwide' in job.description for job in jobs))

    async def test_salary_bounds_currency_and_no_inferred_period(self):
        cases = [
            ({'minSalary': 1234.5, 'currencyCode': 'USD'}, 'USD from 1,234.5'),
            ({'maxSalary': 500}, 'up to 500'),
            ({'minSalary': 0, 'maxSalary': 0, 'currencyCode': 'EUR'}, 'EUR 0'),
            ({'minSalary': 38, 'maxSalary': 45}, '38 - 45'),
            ({}, '-'),
            (None, '-'),
        ]
        self.response.json.return_value = [vacancy(vacancy_id=i, salary=salary) for i, (salary, _) in enumerate(cases, 1)]
        self.assertEqual([job.salary for job in await self.source.fetch(CUTOFF)], [expected for _, expected in cases])

    async def test_empty_list_is_a_success(self):
        self.response.json.return_value = []
        self.assertEqual(await self.source.fetch(CUTOFF), [])

    async def test_malformed_response_or_record_raises_source_error(self):
        records = [None, [], 'invalid', {}, vacancy(vacancy_id=True), vacancy(vacancy_id=-1),
                   vacancy(vacancy_id='2507608'), vacancy(name=None), vacancy(name=' '),
                   vacancy(openingDate='bad'), vacancy(openingDate=123), vacancy(isRemote='false'),
                   vacancy(isWorldWide=1), vacancy(summary='invalid'), vacancy(requirements=[None]),
                   vacancy(requirements=[{'TYPE': 'list', 'content': ['ok', 1]}]),
                   vacancy(niceToHave=[{'TYPE': 'text', 'content': []}]),
                   vacancy(joinUsReason=[{'TYPE': 'unknown', 'content': ''}]),
                   vacancy(summary=None, responsibilities=None, requirements=None, niceToHave=None, joinUsReason=None),
                   vacancy(lookingLocations=['bad']), vacancy(lookingLocations=[{'countryName': 123}]),
                   vacancy(notLookingLocations=[{'countryCode': 'XX', 'cities': [1]}]),
                   vacancy(technologies='Flutter'), vacancy(level=[]), vacancy(salary=[]),
                   vacancy(salary={'minSalary': True}), vacancy(salary={'minSalary': '1000'}),
                   vacancy(salary={'minSalary': float('nan')}), vacancy(salary={'maxSalary': float('inf')}),
                   vacancy(salary={'minSalary': -1}), vacancy(salary={'minSalary': 2, 'maxSalary': 1}),
                   vacancy(salary={'currencyCode': 123})]
        payloads = [None, {}, 'bad', *[[record] for record in records]]
        for payload in payloads:
            with self.subTest(payload=payload):
                self.response.json.return_value = payload
                with self.assertRaisesRegex(AndersenFetchError, 'invalid job data'):
                    await self.source.fetch(CUTOFF)

    async def test_invalid_json_is_reported(self):
        self.response.json.side_effect = ValueError('not JSON')
        with self.assertRaisesRegex(AndersenFetchError, 'invalid job data'):
            await self.source.fetch(CUTOFF)

    async def test_http_errors_and_rate_limit(self):
        for status in (403, 404, 429, 500):
            with self.subTest(status=status):
                response = requests.Response()
                response.status_code = status
                self.response.raise_for_status.side_effect = requests.HTTPError(response=response)
                with self.assertRaisesRegex(AndersenFetchError, f'HTTP {status}') as raised:
                    await self.source.fetch(CUTOFF)
                if status == 429:
                    self.assertIn('rate limit exceeded', str(raised.exception))
                self.response.json.assert_not_called()

    async def test_network_failure(self):
        self.request.side_effect = requests.Timeout('timed out')
        with self.assertRaisesRegex(AndersenFetchError, 'network connectivity'):
            await self.source.fetch(CUTOFF)
