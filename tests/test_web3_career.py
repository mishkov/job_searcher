import hashlib
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import requests

from src.job_sources.web3_career.web3_career import (
    Web3CareerFetchError,
    Web3CareerJobsSource,
    Web3CareerJobsSourceConfig,
)
from src.search_filter import SearchFilter


CUTOFF = datetime(2026, 10, 12, 10, tzinfo=timezone.utc)
TOKEN = 'test-private-token'
APPLY_URL = 'https://web3.career/r/example__tracking?ref=provider'


def vacancy(**overrides):
    # Fields observed in the authenticated API, including its epoch and RFC date.
    record = {
        'id': 151974,
        'title': 'Flutter Engineer',
        'description': '<p>Build Flutter apps.</p><p>Own releases &amp; testing.</p>',
        'date': 'Mon, 12 Oct 2026 11:00:00 +0100',
        'date_epoch': int(CUTOFF.timestamp()),
        'is_remote': True,
        'apply_url': APPLY_URL,
        'tags': ['dev', 'flutter'],
        'salary_min_value': '90000.0',
        'salary_max_value': '120000.0',
        'salary_currency': 'USD',
        'salary_unit': 'YEAR',
    }
    record.update(overrides)
    return record


class Web3CareerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.source = Web3CareerJobsSource(Web3CareerJobsSourceConfig(SearchFilter(), api_token=TOKEN))
        self.response = MagicMock()
        self.response.json.return_value = ['metadata', 'version', [vacancy()]]
        self.request_patch = patch('src.job_sources.web3_career.web3_career.requests.get', return_value=self.response)
        self.request = self.request_patch.start()
        self.addCleanup(self.request_patch.stop)

    async def test_authenticated_flutter_query_and_actual_api_fields(self):
        jobs = await self.source.fetch(CUTOFF)
        self.request.assert_called_once_with(
            'https://web3.career/api/v1',
            params={'token': TOKEN, 'tag': 'flutter', 'limit': 100, 'show_description': 'true'},
            timeout=15,
        )
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual(job.title, 'Flutter Engineer')
        self.assertEqual(job.description, 'Build Flutter apps.\nOwn releases & testing.')
        self.assertEqual(job.salary, 'USD 90,000 - 120,000 / year')
        self.assertTrue(job.is_remote_allowed)
        self.assertEqual(job.url, APPLY_URL)
        self.assertEqual(job.fingerprint, hashlib.sha256(b'web3.career:151974').hexdigest())

    async def test_environment_token_is_read_at_fetch_time(self):
        source = Web3CareerJobsSource(Web3CareerJobsSourceConfig(SearchFilter()))
        with patch.dict(os.environ, {'WEB3_CAREER_API_TOKEN': TOKEN}):
            self.assertEqual(len(await source.fetch(CUTOFF)), 1)
        self.assertEqual(self.request.call_args.kwargs['params']['token'], TOKEN)

    async def test_missing_token_fails_without_request(self):
        source = Web3CareerJobsSource(Web3CareerJobsSourceConfig(SearchFilter()))
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(Web3CareerFetchError, 'Set WEB3_CAREER_API_TOKEN'):
                await source.fetch(CUTOFF)
        self.request.assert_not_called()

    async def test_date_cutoff_includes_boundary_without_assuming_sort_order(self):
        self.response.json.return_value = ['', '', [
            vacancy(id=2, date_epoch=int(CUTOFF.timestamp()) - 1),
            vacancy(),
            vacancy(id=3, date_epoch=int(CUTOFF.timestamp()) + 1),
        ]]
        self.assertEqual(len(await self.source.fetch(CUTOFF)), 2)

    async def test_epoch_takes_priority_and_rfc_dates_support_timezone_offsets(self):
        self.response.json.return_value = [
            vacancy(id=1, date='not a date'),
            vacancy(id=2, date_epoch=None),
            vacancy(id=3, date_epoch=None, date='Mon, 12 Oct 2026 16:59:59 +0700'),
        ]
        self.assertEqual(len(await self.source.fetch(CUTOFF)), 2)

    async def test_documented_iso_date_remote_and_salary_fields(self):
        record = vacancy(postedAt='2026-10-12T13:00:00+03:00', remote=False, salary='$100k')
        for field in ('date_epoch', 'date', 'is_remote'):
            del record[field]
        self.response.json.return_value = [record]
        jobs = await self.source.fetch(CUTOFF.replace(tzinfo=None))
        self.assertEqual(len(jobs), 1)
        self.assertFalse(jobs[0].is_remote_allowed)
        self.assertEqual(jobs[0].salary, '$100k')

    async def test_empty_and_flat_job_arrays(self):
        for payload, count in (([], 0), (['metadata', 'version', []], 0), ([vacancy()], 1)):
            with self.subTest(payload=payload):
                self.response.json.return_value = payload
                self.assertEqual(len(await self.source.fetch(CUTOFF)), count)

    async def test_keywords_match_title_description_or_tags_case_insensitively(self):
        self.source = Web3CareerJobsSource(Web3CareerJobsSourceConfig(SearchFilter(keywords=['FLUTTER']), api_token=TOKEN))
        self.response.json.return_value = [
            vacancy(id=1, description='', tags=[]),
            vacancy(id=2, title='Mobile Engineer', tags=[]),
            vacancy(id=3, title='Mobile Engineer', description=''),
            vacancy(id=4, title='Python Engineer', description='', tags=['python']),
        ]
        self.assertEqual(len(await self.source.fetch(CUTOFF)), 3)

    async def test_exclusion_takes_priority_over_wildcard_or_keyword(self):
        self.response.json.return_value = [vacancy(description='Unpaid Flutter internship.')]
        for keywords in (['*'], ['flutter']):
            with self.subTest(keywords=keywords):
                filters = SearchFilter(keywords=keywords, exclude_keywords=['UNPAID'])
                source = Web3CareerJobsSource(Web3CareerJobsSourceConfig(filters, api_token=TOKEN))
                self.assertEqual(await source.fetch(CUTOFF), [])

    async def test_tracking_changes_do_not_create_duplicate_jobs(self):
        self.response.json.return_value = [vacancy(), vacancy(apply_url='https://web3.career/r/different-tracking')]
        jobs = await self.source.fetch(CUTOFF)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].url, APPLY_URL)

    async def test_canonical_url_preserves_previous_fingerprint_when_available(self):
        canonical = 'https://web3.career/flutter-engineer-example/151974'
        self.response.json.return_value = [vacancy(url=canonical)]
        job = (await self.source.fetch(CUTOFF))[0]
        self.assertEqual(job.fingerprint, hashlib.sha256(canonical.encode()).hexdigest())
        self.assertEqual(job.url, APPLY_URL)

    async def test_optional_fields_and_estimated_salary(self):
        self.response.json.return_value = [
            vacancy(id=1, title=None, description=None, tags=None, is_remote=None,
                    salary_min_value=None, salary_max_value=None),
            vacancy(id=2, salary_min_value=None, salary_max_value=None,
                    estimated_min_salary=100000, estimated_max_salary=140000),
            vacancy(id=3, salary_min_value='0.0', salary_max_value='0.0'),
        ]
        jobs = await self.source.fetch(CUTOFF)
        self.assertEqual((jobs[0].title, jobs[0].description, jobs[0].salary), ('-', '', '-'))
        self.assertIsNone(jobs[0].is_remote_allowed)
        self.assertEqual(jobs[1].salary, '100,000 - 140,000 (estimated)')
        self.assertEqual(jobs[2].salary, 'USD 0 / year')

    async def test_invalid_response_missing_url_and_bad_dates_fail_clearly(self):
        for payload in ({'error': 'bad data'}, ['metadata'], ['', '', ['bad job']],
                        [vacancy(apply_url=None)], [vacancy(date_epoch=None, date='invalid')]):
            with self.subTest(payload=payload):
                self.response.json.return_value = payload
                with self.assertRaisesRegex(Web3CareerFetchError, 'invalid job data'):
                    await self.source.fetch(CUTOFF)

    async def test_invalid_json_fails_clearly(self):
        self.response.json.side_effect = ValueError('not JSON')
        with self.assertRaisesRegex(Web3CareerFetchError, 'invalid job data'):
            await self.source.fetch(CUTOFF)

    async def test_http_errors_do_not_expose_token_in_message_or_traceback(self):
        for status in (401, 403, 429, 500):
            with self.subTest(status=status):
                response = requests.Response()
                response.status_code = status
                self.response.raise_for_status.side_effect = requests.HTTPError(
                    f'HTTP error: https://web3.career/api/v1?token={TOKEN}', response=response,
                )
                with self.assertRaises(Web3CareerFetchError) as raised:
                    await self.source.fetch(CUTOFF)
                self.assertIn(f'HTTP {status}', str(raised.exception))
                self.assertNotIn(TOKEN, str(raised.exception))
                self.assertTrue(raised.exception.__suppress_context__)

    async def test_network_errors_do_not_expose_token(self):
        self.request.side_effect = requests.ConnectionError(f'Could not connect: token={TOKEN}')
        with self.assertRaisesRegex(Web3CareerFetchError, 'network connectivity') as raised:
            await self.source.fetch(CUTOFF)
        self.assertNotIn(TOKEN, str(raised.exception))
        self.assertTrue(raised.exception.__suppress_context__)


if __name__ == '__main__':
    unittest.main()
