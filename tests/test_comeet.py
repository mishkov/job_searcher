import hashlib
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import requests

from src.error_reporting import ErrorReporter
from src.job_sources.comeet.comeet import ComeetFetchError, ComeetJobsSource, ComeetJobsSourceConfig
from src.job_sources.error_reporting_source import ErrorReportingJobsSource
from src.search_filter import SearchFilter


CUTOFF = datetime(2026, 10, 26, 7, tzinfo=timezone.utc)
BOARD_URL = 'https://www.comeet.com/jobs/biocatch/03.00E'
JOB_URL = BOARD_URL + '/mobile-developer/EE.27D'


def posting(**changes):
    record = {
        'name': 'Mobile Developer', 'uid': 'EE.27D', 'department': 'Applications',
        'time_updated': CUTOFF.isoformat(), 'is_internal': False,
        'location': {'name': 'Israel - TLV', 'is_remote': True},
        'workplace_type': 'Hybrid', 'employment_type': 'Full-time', 'experience_level': 'Senior',
        'url_comeet_hosted_page': JOB_URL,
        'custom_fields': {'details': [
            {'name': 'Description', 'value': '<p>Build mobile SDKs &amp; apps.</p>', 'order': 1},
            {'name': 'Requirements', 'value': '<ul><li>FLUTTER &amp; Dart</li></ul>', 'order': 2},
        ]},
    }
    record.update(changes)
    return record


def listing(*records, slug='biocatch', uid='03.00E'):
    company = {'name': 'BioCatch', 'slug': slug, 'company_uid': uid}
    positions = json.dumps(records).replace('<', '\\u003C')
    return '<html><script>\n COMPANY_DATA = ' + json.dumps(company) + ';\n COMPANY_POSITIONS_DATA = ' + positions + ';\n</script></html>'


def response(html, status=200):
    result = Mock(text=html)
    if status >= 400:
        error_response = requests.Response()
        error_response.status_code = status
        result.raise_for_status.side_effect = requests.HTTPError(response=error_response)
    return result


class ComeetConfigTests(unittest.TestCase):
    def test_independent_company_uid_and_filter_history(self):
        configs = [ComeetJobsSourceConfig(slug, uid, SearchFilter([word])) for slug, uid, word in (
            ('biocatch', '03.00E', 'flutter'), ('other', '98.008', 'flutter'),
            ('biocatch', '98.008', 'flutter'), ('biocatch', '03.00E', 'python'))]
        self.assertEqual(len({config.history_key for config in configs}), 4)
        self.assertEqual(configs[0].history_key,
                         ComeetJobsSourceConfig('biocatch', '03.00E', SearchFilter(['FLUTTER'])).history_key)
        self.assertEqual(configs[0].name, 'Comeet: biocatch')

    def test_invalid_company_identifiers(self):
        for slug in ('', None, '../biocatch', 'biocatch/03.00E', BOARD_URL, 'biocatch?x=1'):
            with self.subTest(slug=slug), self.assertRaisesRegex(ValueError, 'company_slug'):
                ComeetJobsSourceConfig(slug, '03.00E', SearchFilter())
        for uid in ('', None, '03', '../03.00E', '03.00E?x=1', '03.00E/jobs'):
            with self.subTest(uid=uid), self.assertRaisesRegex(ValueError, 'company_uid'):
                ComeetJobsSourceConfig('biocatch', uid, SearchFilter())


class ComeetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.source = self.source_with(SearchFilter(['flutter']))
        patcher = patch('src.job_sources.comeet.comeet.requests.get')
        self.request = patcher.start()
        self.addCleanup(patcher.stop)
        self.request.return_value = response(listing(posting()))

    @staticmethod
    def source_with(filters, slug='biocatch', uid='03.00E'):
        return ComeetJobsSource(ComeetJobsSourceConfig(slug, uid, filters))

    async def test_description_requirements_metadata_and_request(self):
        job, = await self.source.fetch(CUTOFF)
        self.assertEqual(job.title, 'Mobile Developer')
        for text in ('Build mobile SDKs & apps.', 'Requirements\nFLUTTER & Dart',
                     'Company: BioCatch', 'Location: Israel - TLV', 'Department: Applications',
                     'Employment: Full-time', 'Experience: Senior', 'Workplace: Hybrid'):
            self.assertIn(text, job.description)
        self.assertNotIn('<p>', job.description)
        self.assertEqual(job.salary, '-')
        self.assertIs(job.is_remote_allowed, True)
        self.assertEqual(job.url, JOB_URL)
        self.request.assert_called_once_with(BOARD_URL, timeout=30)

    async def test_filters_and_exclusion_priority(self):
        for filters, count in ((SearchFilter(['DART']), 1), (SearchFilter(['python']), 0),
                               (SearchFilter(['*']), 1), (SearchFilter(['*'], ['flutter']), 0),
                               (SearchFilter(['flutter'], ['sdk']), 0)):
            with self.subTest(filters=filters):
                self.assertEqual(len(await self.source_with(filters).fetch(CUTOFF)), count)
        self.request.reset_mock()
        self.assertEqual(await self.source_with(SearchFilter([])).fetch(CUTOFF), [])
        self.request.assert_not_called()

    async def test_date_boundary_timezones_and_unsorted_records(self):
        for date, since, count in ((CUTOFF.isoformat(), CUTOFF, 1),
                                  ('2026-10-26T03:00:00-04:00', CUTOFF, 1),
                                  ('2026-10-26T07:00:00', CUTOFF.replace(tzinfo=None), 1),
                                  ((CUTOFF - timedelta(seconds=1)).isoformat(), CUTOFF, 0)):
            with self.subTest(date=date):
                self.request.return_value = response(listing(posting(time_updated=date)))
                self.assertEqual(len(await self.source.fetch(since)), count)
        self.request.return_value = response(listing(
            posting(uid='00.000', time_updated='2020-01-01T00:00:00Z'), posting(), posting()))
        self.assertEqual(len(await self.source.fetch(CUTOFF)), 1)

    async def test_empty_and_internal_jobs_and_fresh_requests(self):
        self.request.side_effect = [response(listing(posting())), response(listing()),
                                    response(listing(posting(is_internal=True, time_updated=None)))]
        self.assertEqual(len(await self.source.fetch(CUTOFF)), 1)
        self.assertEqual(await self.source.fetch(CUTOFF), [])
        self.assertEqual(await self.source.fetch(CUTOFF), [])
        self.assertEqual(self.request.call_count, 3)

    async def test_stable_fingerprints_tracking_and_other_company(self):
        job, = await self.source.fetch(CUTOFF)
        self.assertEqual(job.fingerprint, hashlib.sha256(b'comeet.com:03.00E:EE.27D').hexdigest())
        self.request.return_value = response(listing(posting(name='Renamed', url_comeet_hosted_page=JOB_URL + '?t=123#apply')))
        renamed, = await self.source.fetch(CUTOFF)
        self.assertEqual(job.fingerprint, renamed.fingerprint)
        self.assertEqual(renamed.url, JOB_URL)
        url = 'https://www.comeet.com/jobs/other/98.008/engineer/EE.27D'
        self.request.return_value = response(listing(posting(url_comeet_hosted_page=url), slug='other', uid='98.008'))
        other, = await self.source_with(SearchFilter(), 'other', '98.008').fetch(CUTOFF)
        self.assertNotEqual(job.fingerprint, other.fingerprint)
        self.assertEqual(self.request.call_args.args, ('https://www.comeet.com/jobs/other/98.008',))

    async def test_location_variants_and_remote_flags(self):
        for workplace, location, expected in (('Remote', None, True), ('Hybrid', None, True),
                                               ('On-site', {'is_remote': True}, False),
                                               (None, {'is_remote': False}, False), (None, None, None)):
            with self.subTest(workplace=workplace, location=location):
                self.request.return_value = response(listing(posting(workplace_type=workplace, location=location)))
                job, = await self.source.fetch(CUTOFF)
                self.assertIs(job.is_remote_allowed, expected)

    async def test_multi_location_position_uids_and_empty_sections(self):
        self.request.return_value = response(listing(posting(
            uid='EE.27D-12.40D', url_comeet_hosted_page=JOB_URL + '-12.40D',
            custom_fields={'details': [{'name': 'Empty', 'value': None},
                                       {'name': 'Description', 'value': '<p>Flutter</p><script>secret</script>'}]})))
        job, = await self.source.fetch(CUTOFF)
        self.assertNotIn('secret', job.description)
        self.assertEqual(job.url, JOB_URL + '-12.40D')

    async def test_malformed_data_fails_instead_of_advancing_history(self):
        invalid_records = [None, {}, 'bad', posting(uid='bad/uid'), posting(time_updated='bad'),
                           posting(time_updated=None), posting(name=''), posting(is_internal='false'),
                           posting(location=[]), posting(location={'is_remote': 'true'}, workplace_type=None),
                           posting(custom_fields=None), posting(custom_fields={'details': 'bad'}),
                           posting(custom_fields={'details': [{'name': 'Description', 'value': '<p></p>'}]}),
                           posting(url_comeet_hosted_page='javascript:alert(1)'),
                           posting(url_comeet_hosted_page=JOB_URL.replace('biocatch', 'other')),
                           posting(url_comeet_hosted_page=JOB_URL.replace('www.comeet.com', 'user:pass@www.comeet.com'))]
        invalid_pages = ['<html/>', '<script>COMPANY_DATA = {};</script>',
                         '<script>COMPANY_DATA = invalid;</script>',
                         listing(posting(), slug='other'), listing(posting(), uid='98.008'),
                         listing().replace('COMPANY_POSITIONS_DATA = []', 'COMPANY_POSITIONS_DATA = null')]
        for html in invalid_pages + [listing(record) for record in invalid_records]:
            with self.subTest(html=html):
                self.request.return_value = response(html)
                with self.assertRaisesRegex(ComeetFetchError, 'invalid job data'):
                    await self.source.fetch(CUTOFF)

    async def test_http_and_network_failures(self):
        for status in (403, 404, 429, 500):
            with self.subTest(status=status):
                self.request.return_value = response('', status)
                with self.assertRaisesRegex(ComeetFetchError, f'HTTP {status}') as raised:
                    await self.source.fetch(CUTOFF)
                if status == 429:
                    self.assertIn('rate limit exceeded', str(raised.exception))
        self.request.side_effect = requests.Timeout()
        with self.assertRaisesRegex(ComeetFetchError, 'network connectivity'):
            await self.source.fetch(CUTOFF)

    async def test_failure_context_identifies_company_and_posting(self):
        self.request.return_value = response(listing(posting(custom_fields=None)))
        reporter = Mock(spec=ErrorReporter)
        with self.assertRaises(ComeetFetchError):
            await ErrorReportingJobsSource(self.source, reporter).fetch(CUTOFF)
        reporter.report.assert_called_once()
        context = reporter.report.call_args.kwargs['context']
        self.assertEqual(context['source'], 'Comeet: biocatch')
        self.assertEqual(context['operation'], 'parse_posting')
        self.assertEqual(context['posting']['id'], 'EE.27D')
