import hashlib
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import requests

from src.job_sources.remote_ok.remote_ok import (
    RemoteOkFetchError,
    RemoteOkJobsSource,
    RemoteOkJobsSourceConfig,
)
from src.search_filter import SearchFilter


CUTOFF = datetime(2026, 10, 12, 10, tzinfo=timezone.utc)
METADATA = {'last_updated': 1791799200, 'legal': 'Link back and mention Remote OK as a source.'}


def vacancy(**overrides):
    record = {
        'id': '123456',
        'epoch': CUTOFF.timestamp(),
        'date': '2026-10-12T10:00:00+00:00',
        'position': 'Flutter Engineer',
        'company': 'Example Inc',
        'location': 'Europe',
        'tags': ['flutter', 'mobile'],
        'description': '<p>Build apps &amp; platforms.</p><ul><li>Own releases.</li></ul>',
        'salary_min': 90000,
        'salary_max': 120000,
        'url': 'https://remoteOK.com/remote-jobs/remote-flutter-engineer-example-123456',
        'apply_url': 'https://example.com/apply',
    }
    record.update(overrides)
    return record


class RemoteOkTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.source = self.source_with(SearchFilter(keywords=['flutter']))
        self.response = MagicMock()
        self.response.json.return_value = [METADATA, vacancy()]
        request_patch = patch(
            'src.job_sources.remote_ok.remote_ok.requests.get', return_value=self.response,
        )
        self.request = request_patch.start()
        self.addCleanup(request_patch.stop)

    def source_with(self, filters):
        return RemoteOkJobsSource(RemoteOkJobsSourceConfig(filters))

    async def test_official_free_api_fields_and_attribution(self):
        jobs = await self.source.fetch(CUTOFF)
        self.request.assert_called_once_with('https://remoteok.com/api', timeout=15)
        self.response.raise_for_status.assert_called_once()
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual(job.title, 'Flutter Engineer')
        self.assertTrue(job.description.startswith('Source: Remote OK\n'))
        self.assertIn('Company: Example Inc', job.description)
        self.assertIn('Location: Europe', job.description)
        self.assertIn('Tags: flutter, mobile', job.description)
        self.assertIn('Build apps & platforms.\nOwn releases.', job.description)
        self.assertEqual(job.salary, '90,000 - 120,000')
        self.assertTrue(job.is_remote_allowed)
        self.assertEqual(job.url, vacancy()['url'])
        self.assertNotEqual(job.url, vacancy()['apply_url'])
        self.assertEqual(job.fingerprint, hashlib.sha256(b'remoteok.com:123456').hexdigest())

    async def test_attribution_survives_telegram_description_preview(self):
        self.response.json.return_value = [METADATA, vacancy(description='<p>' + 'Flutter ' * 200 + '</p>')]
        job = (await self.source.fetch(CUTOFF))[0]
        self.assertIn('Source: Remote OK', job.description[:500])

    async def test_cutoff_is_inclusive_and_unsorted_feed_is_processed(self):
        self.response.json.return_value = [METADATA,
            vacancy(id='old', epoch=CUTOFF.timestamp() - 1),
            vacancy(id='boundary'),
            vacancy(id='new', epoch=CUTOFF.timestamp() + 1),
        ]
        for cutoff in (CUTOFF, CUTOFF.replace(tzinfo=None),
                       CUTOFF.astimezone(timezone(timedelta(hours=3)))):
            with self.subTest(cutoff=cutoff):
                self.assertEqual(len(await self.source.fetch(cutoff)), 2)

    async def test_iso_date_fallback_and_epoch_takes_priority(self):
        self.response.json.return_value = [
            vacancy(id='iso', epoch=None, date='2026-10-12T13:00:00+03:00'),
            vacancy(id='naive', epoch=None, date='2026-10-12T10:00:00'),
            vacancy(id='epoch', date='2000-01-01T00:00:00Z'),
            vacancy(id='old', epoch=None, date='2026-10-12T09:59:59Z'),
        ]
        self.assertEqual(len(await self.source.fetch(CUTOFF)), 3)

    async def test_keywords_match_title_description_tags_company_and_location(self):
        for field, value in (
            ('position', 'FLUTTER Engineer'), ('description', '<p>FLUTTER apps</p>'),
            ('tags', ['FLUTTER']), ('company', 'FLUTTER Inc'), ('location', 'FLUTTER City'),
        ):
            with self.subTest(field=field):
                record = vacancy(position='Engineer', description='', tags=[], company='', location='')
                record[field] = value
                self.response.json.return_value = [METADATA, record]
                self.assertEqual(len(await self.source.fetch(CUTOFF)), 1)
        self.response.json.return_value = [vacancy(position='Engineer', description='', tags=[])]
        self.assertEqual(await self.source.fetch(CUTOFF), [])

    async def test_exclusions_override_keywords_and_wildcard(self):
        self.response.json.return_value = [vacancy(description='<p>Unpaid Flutter internship</p>')]
        for keywords in (['flutter'], ['*']):
            source = self.source_with(SearchFilter(keywords=keywords, exclude_keywords=['UNPAID']))
            self.assertEqual(await source.fetch(CUTOFF), [])

    async def test_wildcard_and_empty_keywords(self):
        self.response.json.return_value = [vacancy(position='Engineer', description='', tags=[])]
        self.assertEqual(len(await self.source_with(SearchFilter()).fetch(CUTOFF)), 1)
        self.request.reset_mock()
        self.assertEqual(await self.source_with(SearchFilter(keywords=[])).fetch(CUTOFF), [])
        self.request.assert_not_called()

    async def test_duplicate_ids_and_url_changes_have_stable_fingerprints(self):
        original = (await self.source.fetch(CUTOFF))[0]
        changed = vacancy(id=123456, url='https://remoteok.com/remote-jobs/renamed-123456?ref=api')
        self.response.json.return_value = [METADATA, changed, vacancy()]
        jobs = await self.source.fetch(CUTOFF)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].fingerprint, original.fingerprint)
        self.assertEqual(jobs[0].url, changed['url'])

    async def test_empty_feed_and_metadata_only_feed(self):
        for payload in ([], [METADATA], [{'last_updated': 123}]):
            with self.subTest(payload=payload):
                self.response.json.return_value = payload
                self.assertEqual(await self.source.fetch(CUTOFF), [])

    async def test_missing_optional_fields(self):
        self.response.json.return_value = [vacancy(
            description=None, company=None, location=None, tags=None,
            salary_min=None, salary_max=None,
        )]
        job = (await self.source.fetch(CUTOFF))[0]
        self.assertEqual(job.description, 'Source: Remote OK\n')
        self.assertEqual(job.salary, '-')

    async def test_salary_missing_zero_one_sided_equal_and_fractional_bounds(self):
        values = [(0, 0), (None, None), (90000, 0), (0, 120000), (90000, 90000),
                  ('90000', '120000'), (38.5, 45.75)]
        self.response.json.return_value = [
            vacancy(id=str(index), salary_min=minimum, salary_max=maximum)
            for index, (minimum, maximum) in enumerate(values)
        ]
        self.assertEqual([job.salary for job in await self.source.fetch(CUTOFF)], [
            '-', '-', 'from 90,000', 'up to 120,000', '90,000', '90,000 - 120,000', '38.5 - 45.75',
        ])

    async def test_invalid_payloads_and_job_fields_fail_clearly(self):
        payloads = [None, {}, ['bad'], [METADATA, []], [METADATA, {}]]
        for fields in (
            {'epoch': 'bad'}, {'epoch': float('inf')}, {'epoch': None, 'date': 'bad'},
            {'id': None}, {'id': ''}, {'id': True}, {'url': None},
            {'url': 'javascript:alert(1)'}, {'url': 'https://example.com/remote-jobs/123'},
            {'url': 'https://remoteok.com.evil.example/remote-jobs/123'},
            {'url': 'https://remoteok.com@evil.example/remote-jobs/123'},
            {'url': 'https://remoteok.com/'}, {'tags': ['flutter', 1]}, {'tags': 'flutter'}, {'tags': {}},
            {'position': []}, {'description': {}}, {'company': 123}, {'location': []},
            {'salary_min': -1}, {'salary_max': float('nan')}, {'salary_min': True},
        ):
            payloads.append([METADATA, vacancy(**fields)])
        for payload in payloads:
            with self.subTest(payload=payload):
                self.response.json.return_value = payload
                with self.assertRaisesRegex(RemoteOkFetchError, 'invalid job data'):
                    await self.source.fetch(CUTOFF)

    async def test_invalid_json_fails_clearly(self):
        self.response.json.side_effect = ValueError('not JSON')
        with self.assertRaisesRegex(RemoteOkFetchError, 'invalid job data'):
            await self.source.fetch(CUTOFF)

    async def test_http_errors_and_rate_limit_message(self):
        for status in (403, 429, 500):
            with self.subTest(status=status):
                failed = requests.Response()
                failed.status_code = status
                self.response.raise_for_status.side_effect = requests.HTTPError(response=failed)
                with self.assertRaisesRegex(RemoteOkFetchError, f'HTTP {status}') as raised:
                    await self.source.fetch(CUTOFF)
                if status == 429:
                    self.assertIn('rate limit exceeded', str(raised.exception))

    async def test_network_failures(self):
        for error in (requests.Timeout('timeout'), requests.ConnectionError('disconnected')):
            with self.subTest(error=error):
                self.request.side_effect = error
                with self.assertRaisesRegex(RemoteOkFetchError, 'network connectivity'):
                    await self.source.fetch(CUTOFF)


if __name__ == '__main__':
    unittest.main()
