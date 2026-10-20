import hashlib
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import requests

from src.error_reporting import ErrorReporter
from src.job_sources.error_reporting_source import ErrorReportingJobsSource
from src.job_sources.smartrecruiters.smartrecruiters import (
    SmartRecruitersFetchError, SmartRecruitersJobsSource, SmartRecruitersJobsSourceConfig,
)
from src.search_filter import SearchFilter


CUTOFF = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)
DETAIL_URL = 'https://api.smartrecruiters.com/v1/companies/Example/postings/13637588010'
JOB_URL = 'https://jobs.smartrecruiters.com/Example/744000155741822-flutter-developer'


def listing(**overrides):
    record = {
        'id': '744000155741822', 'name': 'Flutter Developer',
        'company': {'identifier': 'Example', 'name': 'Example Company'},
        'releasedDate': CUTOFF.isoformat(),
        'actions': {'details': DETAIL_URL}, 'applyUrl': JOB_URL,
        'location': {'remote': False, 'hybrid': True}, 'shortLocation': 'Mumbai, India',
    }
    return {**record, **overrides}


def detail(**overrides):
    return {
        'id': '13637588010', 'name': 'Flutter Developer',
        'releasedDate': CUTOFF.isoformat(), 'active': True, 'visibility': 'PUBLIC',
        'company': {'identifier': 'Example', 'name': 'Example Company'},
        'location': {'remote': False, 'hybrid': True, 'fullLocation': 'Mumbai, India',
                     'hybridDescription': 'Three days in the office'},
        'jobAd': {'sections': {
            'companyDescription': {'title': 'Company Description', 'text': '<p>Apps &amp; services</p>'},
            'jobDescription': {'title': 'Job Description', 'text': '<p>Build <b>Flutter</b> apps.</p>'},
            'qualifications': {'title': 'Qualifications', 'text': '<ul><li>Dart experience</li></ul>'},
            'additionalInformation': {'title': 'Additional Information', 'text': ''},
            'videos': {'urls': ['https://example.com/video']},
        }},
        'typeOfEmployment': {'label': 'Full-time'}, 'experienceLevel': {'label': 'Mid-Senior Level'},
        **overrides,
    }


def response(payload, status=200):
    return Mock(status_code=status, json=Mock(return_value=payload))


class SmartRecruitersTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.source = self.source_with(SearchFilter(['flutter']))
        patcher = patch('src.job_sources.smartrecruiters.smartrecruiters.requests.get')
        self.request = patcher.start()
        self.addCleanup(patcher.stop)
        self.request.side_effect = [response({'content': [listing()]}), response(detail())]

    @staticmethod
    def source_with(filters):
        return SmartRecruitersJobsSource(SmartRecruitersJobsSourceConfig(filters))

    async def test_live_search_and_detail_shapes_map_full_job(self):
        job, = await self.source.fetch(CUTOFF)
        self.assertEqual(self.request.call_args_list[0].args, (self.source.API_URL,))
        self.assertEqual(self.request.call_args_list[0].kwargs,
                         {'params': {'keyword': 'flutter', 'limit': 100}, 'timeout': 30})
        self.assertEqual(self.request.call_args_list[1].args, (DETAIL_URL,))
        self.assertEqual(job.title, 'Flutter Developer')
        for text in ('Apps & services', 'Build', 'Flutter', 'Dart experience', 'Company: Example Company',
                     'Location: Mumbai, India', 'Workplace: Hybrid', 'Three days in the office',
                     'Employment: Full-time', 'Experience: Mid-Senior Level'):
            self.assertIn(text, job.description)
        self.assertNotIn('<b>', job.description)
        self.assertTrue(job.is_remote_allowed)
        self.assertEqual(job.salary, '-')
        self.assertEqual(job.url, JOB_URL)
        self.assertEqual(job.fingerprint,
                         hashlib.sha256(b'smartrecruiters.com:Example:744000155741822').hexdigest())

    async def test_multiple_keywords_union_and_duplicates_fetch_details_once(self):
        self.request.side_effect = [response({'content': [listing(), listing()]}), response(detail()),
                                    response({'content': [listing()]})]
        jobs = await self.source_with(SearchFilter(['flutter', 'dart', 'flutter'])).fetch(CUTOFF)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(self.request.call_count, 3)
        self.assertEqual(self.request.call_args.kwargs['params']['keyword'], 'dart')

    async def test_dates_are_inclusive_normalize_timezone_and_do_not_assume_order(self):
        for cutoff in (CUTOFF, CUTOFF.replace(tzinfo=None), CUTOFF.astimezone(timezone(timedelta(hours=3)))):
            with self.subTest(cutoff=cutoff):
                self.request.side_effect = [response({'content': [
                    listing(id='1', releasedDate='2026-10-15T11:59:59Z'),
                    listing(id='2', releasedDate='2026-10-15T15:00:00+03:00'),
                    listing(id='3', releasedDate='2026-10-15T12:00:01'),
                ]}), response(detail()), response(detail())]
                self.assertEqual(len(await self.source.fetch(cutoff)), 2)

    async def test_full_text_filters_and_exclusions_take_priority_over_wildcard(self):
        for keywords, exclusions, expected in [(['DART'], [], 1), (['python'], [], 0),
                                                (['flutter'], ['SERVICES'], 0), (['*'], ['dart'], 0)]:
            with self.subTest(keywords=keywords, exclusions=exclusions):
                self.request.side_effect = [response({'content': [listing()]}), response(detail())]
                self.assertEqual(len(await self.source_with(SearchFilter(keywords, exclusions)).fetch(CUTOFF)), expected)

    async def test_empty_keywords_and_wildcard_queries(self):
        self.assertEqual(await self.source_with(SearchFilter([])).fetch(CUTOFF), [])
        self.request.assert_not_called()
        await self.source_with(SearchFilter(['*', 'flutter'])).fetch(CUTOFF)
        self.assertEqual(self.request.call_args_list[0].kwargs['params'], {'limit': 100})

    async def test_empty_search_and_missing_removed_private_or_stale_details(self):
        self.request.side_effect = [response({'content': []})]
        self.assertEqual(await self.source.fetch(CUTOFF), [])
        for payload, status in [(None, 404), (None, 410), (detail(active=False), 200),
                                (detail(visibility='PRIVATE'), 200),
                                (detail(releasedDate='2020-01-01T00:00:00Z'), 200)]:
            with self.subTest(status=status, payload=payload):
                self.request.side_effect = [response({'content': [listing()]}), response(payload, status)]
                self.assertEqual(await self.source.fetch(CUTOFF), [])

    async def test_remote_hybrid_onsite_and_unknown(self):
        for location, expected in [({'remote': True}, True), ({'remote': False, 'hybrid': True}, True),
                                    ({'remote': False}, False), ({}, None)]:
            with self.subTest(location=location):
                self.request.side_effect = [response({'content': [listing()]}), response(detail(location=location))]
                job, = await self.source.fetch(CUTOFF)
                self.assertIs(job.is_remote_allowed, expected)

    async def test_fingerprint_stays_stable_after_title_and_url_changes(self):
        first, = await self.source.fetch(CUTOFF)
        self.request.side_effect = [response({'content': [listing(applyUrl=JOB_URL + '?tracking=changed')]}),
                                    response(detail(name='Senior Flutter Developer'))]
        second, = await self.source.fetch(CUTOFF)
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(second.url, JOB_URL + '?tracking=changed')

    async def test_invalid_listing_and_detail_data_raise_source_error(self):
        invalid_records = [None, [], {}, listing(id=None), listing(company=[]), listing(releasedDate='bad'),
                           listing(applyUrl='https://example.com/job'),
                           listing(actions={'details': 'https://example.com/internal'})]
        for record in invalid_records:
            with self.subTest(record=record):
                self.request.side_effect = [response({'content': [record]})]
                with self.assertRaisesRegex(SmartRecruitersFetchError, 'invalid job data'):
                    await self.source.fetch(CUTOFF)
        for payload in [None, [], {}, detail(name=''), detail(jobAd={}), detail(jobAd={'sections': []}),
                        detail(jobAd={'sections': {'jobDescription': {'text': []}}}),
                        detail(location={'remote': 'false'}), detail(active='true')]:
            with self.subTest(payload=payload):
                self.request.side_effect = [response({'content': [listing()]}), response(payload)]
                with self.assertRaisesRegex(SmartRecruitersFetchError, 'invalid job data'):
                    await self.source.fetch(CUTOFF)

    async def test_http_network_and_json_failures(self):
        for status in (403, 404, 429, 500):
            with self.subTest(status=status):
                failed = response(None, status)
                error_response = requests.Response()
                error_response.status_code = status
                failed.raise_for_status.side_effect = requests.HTTPError(response=error_response)
                self.request.side_effect = [failed]
                with self.assertRaisesRegex(SmartRecruitersFetchError, f'HTTP {status}'):
                    await self.source.fetch(CUTOFF)
        self.request.side_effect = requests.Timeout('timed out')
        with self.assertRaisesRegex(SmartRecruitersFetchError, 'network connectivity'):
            await self.source.fetch(CUTOFF)
        invalid = response(None)
        invalid.json.side_effect = ValueError('bad JSON')
        self.request.side_effect = [invalid]
        with self.assertRaisesRegex(SmartRecruitersFetchError, 'invalid job data'):
            await self.source.fetch(CUTOFF)

    async def test_detail_failure_reports_posting_and_keyword(self):
        self.request.side_effect = [response({'content': [listing()]}), requests.Timeout('timed out')]
        reporter = Mock(spec=ErrorReporter)
        with self.assertRaises(SmartRecruitersFetchError):
            await ErrorReportingJobsSource(self.source, reporter).fetch(CUTOFF)
        reporter.report.assert_called_once()
        context = reporter.report.call_args.kwargs['context']
        self.assertEqual(context['source'], 'SmartRecruiters')
        self.assertEqual(context['keyword'], 'flutter')
        self.assertEqual(context['operation'], 'fetch_posting')
        self.assertEqual(context['posting']['url'], JOB_URL)
