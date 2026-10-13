import hashlib
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch
from xml.sax.saxutils import escape

import requests

from src.job_sources.we_work_remotely.api import (
    CATEGORIES, FIELDS, FeedResponse, WeWorkRemotelyApi, WeWorkRemotelyFetchError,
)
from src.job_sources.we_work_remotely.python_requests_api import PythonRequestsModuleWeWorkRemotelyApi
from src.job_sources.we_work_remotely.we_work_remotely import (
    WeWorkRemotelyJobsSource, WeWorkRemotelyJobsSourceConfig,
)
from src.search_filter import SearchFilter


CUTOFF = datetime(2026, 10, 12, 10, tzinfo=timezone.utc)
URL = 'https://weworkremotely.com/remote-jobs/example-mobile-engineer'


def vacancy(**overrides):
    record = dict.fromkeys(FIELDS, '')
    record.update({
        'title': 'Example: Mobile Engineer', 'link': URL, 'guid': URL,
        'description': '<p>Build FLUTTER apps &amp; platforms.</p><ul><li>Own releases.</li></ul>',
        'pubDate': 'Mon, 12 Oct 2026 10:00:00 +0000',
        'expires_at': 'Thu, 12 Nov 2026 10:00:00 +0000',
        'region': 'Europe Only', 'country': 'Poland, Germany', 'state': 'Berlin',
        'category': 'Full-Stack Programming', 'type': 'Contract', 'skills': 'mobile',
    })
    record.update(overrides)
    return record


def rss(*records):
    return ('<rss version="2.0"><channel><title>WWR</title>' + ''.join(
        '<item>' + ''.join(f'<{key}>{escape(value)}</{key}>' for key, value in record.items()) + '</item>'
        for record in records
    ) + '</channel></rss>').encode()


class WeWorkRemotelyApiTests(unittest.TestCase):
    def setUp(self):
        self.api = PythonRequestsModuleWeWorkRemotelyApi()
        self.response = Mock(status_code=200, content=rss(vacancy()), headers={'ETag': 'W/"one"'})
        request_patch = patch(
            'src.job_sources.we_work_remotely.python_requests_api.requests.get', return_value=self.response,
        )
        self.request = request_patch.start()
        self.addCleanup(request_patch.stop)

    def test_category_rss_preserves_full_description_metadata_and_validators(self):
        result = self.api.get_feed(CATEGORIES[2])
        self.assertEqual(result, FeedResponse([vacancy()], 'W/"one"'))
        self.request.assert_called_once_with(
            'https://weworkremotely.com/categories/remote-full-stack-programming-jobs.rss',
            headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/rss+xml'}, timeout=30,
        )
        self.response.raise_for_status.assert_called_once()

    def test_no_ten_job_cap_and_empty_feed(self):
        self.response.content = rss(*[vacancy(link=URL + str(i)) for i in range(15)])
        self.assertEqual(len(self.api.get_feed(CATEGORIES[2]).jobs), 15)
        self.response.content = rss()
        self.assertEqual(self.api.get_feed(CATEGORIES[2]).jobs, [])

    def test_cdata_description_and_missing_optional_fields(self):
        self.response.content = b'''<rss><channel><item>
        <title>Engineer</title><description><![CDATA[<p>Flutter &amp; Dart</p>]]></description>
        <pubDate>Mon, 12 Oct 2026 10:00:00 GMT</pubDate>
        <link>https://weworkremotely.com/remote-jobs/engineer</link>
        </item></channel></rss>'''
        record = self.api.get_feed(CATEGORIES[0]).jobs[0]
        self.assertEqual(record['description'], '<p>Flutter &amp; Dart</p>')
        self.assertEqual(record['expires_at'], '')
        self.assertEqual(record['skills'], '')

    def test_conditional_request_and_304_do_not_parse_a_body(self):
        self.response.status_code = 304
        self.response.content = b''
        result = self.api.get_feed(CATEGORIES[0], etag='W/"one"', last_modified='Mon, 12 Oct 2026 10:00:00 GMT')
        self.assertIsNone(result.jobs)
        headers = self.request.call_args.kwargs['headers']
        self.assertEqual(headers['If-None-Match'], 'W/"one"')
        self.assertEqual(headers['If-Modified-Since'], 'Mon, 12 Oct 2026 10:00:00 GMT')
        with self.assertRaisesRegex(WeWorkRemotelyFetchError, 'unexpected HTTP 304'):
            self.api.get_feed(CATEGORIES[0])

    def test_invalid_xml_schema_fields_dates_and_listing_urls_fail(self):
        payloads = [b'not xml', b'<html/>', b'<rss/>', b'<rss><channel><item/></channel></rss>',
                    b'<!DOCTYPE rss [<!ENTITY test "x">]><rss><channel/></rss>']
        for fields in ({'pubDate': 'bad'}, {'expires_at': 'bad'}, {'title': ''}, {'description': ''},
                       {'link': 'javascript:alert(1)'}, {'link': 'https://weworkremotely.com/'},
                       {'link': 'https://weworkremotely.com.evil.example/remote-jobs/job'},
                       {'link': 'https://user@weworkremotely.com/remote-jobs/job'}):
            payloads.append(rss(vacancy(**fields)))
        for payload in payloads:
            with self.subTest(payload=payload):
                self.response.content = payload
                with self.assertRaisesRegex(WeWorkRemotelyFetchError, 'invalid RSS job data'):
                    self.api.get_feed(CATEGORIES[0])

    def test_http_and_network_failures_are_source_errors(self):
        for status in (403, 429, 500):
            response = requests.Response()
            response.status_code = status
            self.response.raise_for_status.side_effect = requests.HTTPError(response=response)
            with self.assertRaisesRegex(WeWorkRemotelyFetchError, f'HTTP {status}') as raised:
                self.api.get_feed(CATEGORIES[0])
            if status == 429:
                self.assertIn('rate limit', str(raised.exception))
        for error in (requests.Timeout(), requests.ConnectionError()):
            self.request.side_effect = error
            with self.assertRaisesRegex(WeWorkRemotelyFetchError, 'network connectivity'):
                self.api.get_feed(CATEGORIES[0])


class WeWorkRemotelySourceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.api = Mock(spec=WeWorkRemotelyApi)
        self.feeds = {category: [] for category in CATEGORIES}
        self.feeds[CATEGORIES[2]] = [vacancy()]
        self.api.get_feed.side_effect = lambda category: FeedResponse(self.feeds[category])
        clock_patch = patch('src.job_sources.we_work_remotely.we_work_remotely.datetime', wraps=datetime)
        self.clock = clock_patch.start()
        self.clock.now.return_value = CUTOFF + timedelta(days=1)
        self.addCleanup(clock_patch.stop)

    def source(self, filters=SearchFilter(keywords=['flutter'])):
        return WeWorkRemotelyJobsSource(WeWorkRemotelyJobsSourceConfig(filters), self.api)

    async def test_all_categories_attribution_remote_location_full_copy_and_salary(self):
        jobs = await self.source().fetch(CUTOFF)
        self.assertEqual([call.args[0] for call in self.api.get_feed.call_args_list], list(CATEGORIES))
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual(job.title, 'Example: Mobile Engineer')
        self.assertTrue(job.description.startswith('Source: We Work Remotely\n'))
        self.assertIn('Countries: Poland, Germany', job.description)
        self.assertIn('Region: Europe Only', job.description)
        self.assertIn('Build FLUTTER apps & platforms.\nOwn releases.', job.description)
        self.assertTrue(job.is_remote_allowed)
        self.assertEqual(job.salary, '-')
        self.assertEqual(job.url, URL)
        self.assertEqual(job.fingerprint, hashlib.sha256(URL.encode()).hexdigest())

    async def test_unsorted_dates_inclusive_cutoff_naive_and_offset_cutoffs(self):
        self.feeds[CATEGORIES[2]] = [
            vacancy(link=URL + '-old', pubDate='Mon, 12 Oct 2026 09:59:59 +0000'),
            vacancy(), vacancy(link=URL + '-new', pubDate='Mon, 12 Oct 2026 13:00:01 +0300'),
        ]
        for cutoff in (CUTOFF, CUTOFF.replace(tzinfo=None), CUTOFF.astimezone(timezone(timedelta(hours=3)))):
            self.assertEqual(len(await self.source().fetch(cutoff)), 2)

    async def test_expired_and_removed_jobs_are_excluded(self):
        self.feeds[CATEGORIES[2]] = [vacancy(expires_at='Tue, 13 Oct 2026 10:00:00 +0000')]
        self.assertEqual(await self.source().fetch(CUTOFF), [])
        self.feeds[CATEGORIES[2]] = [vacancy(expires_at='')]
        self.assertEqual(len(await self.source().fetch(CUTOFF)), 1)
        self.feeds[CATEGORIES[2]] = []
        self.assertEqual(await self.source().fetch(CUTOFF), [])

    async def test_duplicates_across_categories_query_and_fragment_have_one_identity(self):
        self.feeds[CATEGORIES[0]] = [vacancy(link=URL + '/?ref=rss#apply')]
        self.feeds[CATEGORIES[2]] = [vacancy()]
        jobs = await self.source().fetch(CUTOFF)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].url, URL + '/?ref=rss#apply')
        self.assertEqual(jobs[0].fingerprint, hashlib.sha256(URL.encode()).hexdigest())

    async def test_keywords_description_metadata_exclusions_wildcard_and_empty(self):
        for field in ('title', 'description', 'skills', 'country'):
            record = vacancy(title='Engineer', description='<p>Build apps</p>', skills='', country='')
            record[field] = 'FLUTTER'
            self.feeds[CATEGORIES[2]] = [record]
            self.assertEqual(len(await self.source().fetch(CUTOFF)), 1)
        self.assertEqual(await self.source(SearchFilter(exclude_keywords=['flutter'])).fetch(CUTOFF), [])
        self.assertEqual(len(await self.source(SearchFilter()).fetch(CUTOFF)), 1)
        self.api.reset_mock()
        self.assertEqual(await self.source(SearchFilter(keywords=[])).fetch(CUTOFF), [])
        self.api.get_feed.assert_not_called()

    async def test_failure_in_later_category_propagates_instead_of_returning_partial_jobs(self):
        self.api.get_feed.side_effect = [FeedResponse([vacancy()]), WeWorkRemotelyFetchError('HTTP 429')]
        with self.assertRaisesRegex(WeWorkRemotelyFetchError, 'HTTP 429'):
            await self.source().fetch(CUTOFF)
