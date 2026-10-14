import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock, patch

from src.error_reporting import ErrorReporter
from src.job_sources.error_reporting_source import ErrorReportingJobsSource
from src.job_sources.geek_job_ru.geek_job_ru import GeekJobRuJobsSource, GeekJobRuJobsSourceConfig
from src.job_sources.nodesk.api import NoDeskApi, NoDeskFetchError
from src.job_sources.nodesk.nodesk import NoDeskJobsSource, NoDeskJobsSourceConfig
from src.job_sources.relocate_me.api import RelocateMeApi, RelocateMeFetchError
from src.job_sources.relocate_me.relocate_me import RelocateMeJobsSource, RelocateMeJobsSourceConfig
from src.job_sources.telegram_channel.telegram_channel import TelegramChannelJobsSource, TelegramChannelJobsSourceConfig
from src.job_sources.we_work_remotely.api import WeWorkRemotelyFetchError
from src.job_sources.we_work_remotely.python_requests_api import PythonRequestsModuleWeWorkRemotelyApi
from src.job_sources.we_work_remotely.we_work_remotely import WeWorkRemotelyJobsSource, WeWorkRemotelyJobsSourceConfig
from src.job_sources.welcome_to_the_jungle.welcome_to_the_jungle import (
    WelcomeToTheJungleFetchError, WelcomeToTheJungleJobsSource, WelcomeToTheJungleJobsSourceConfig,
)
from src.search_filter import SearchFilter


CUTOFF = datetime(2026, 10, 12, tzinfo=timezone.utc)


class SourceReportingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.reporter = Mock(spec=ErrorReporter)
        self.filters = SearchFilter()

    async def fetch_failure(self, source, error_type):
        with self.assertRaises(error_type):
            await ErrorReportingJobsSource(source, self.reporter).fetch(CUTOFF)
        self.reporter.report.assert_called_once()
        return self.reporter.report.call_args.kwargs['context']

    async def test_nodesk_detail_failure_identifies_listing(self):
        api = Mock(spec=NoDeskApi)
        api.get_jobs_page.return_value = {'results': [{'hits': [{
            'objectID': 'job-123', 'title': 'Engineer', 'datePublished': CUTOFF.isoformat(),
            'permalink': '/remote-jobs/engineer/',
        }], 'nbPages': 1, 'nbHits': 1, 'page': 0}]}
        api.get_job_posting.side_effect = NoDeskFetchError('detail timeout')
        context = await self.fetch_failure(NoDeskJobsSource(NoDeskJobsSourceConfig(self.filters), api), NoDeskFetchError)
        self.assertEqual(context['page'], 0)
        self.assertEqual(context['posting']['objectID'], 'job-123')
        self.assertEqual(context['operation'], 'fetch_posting')

    async def test_later_nodesk_listing_failure_has_no_previous_posting(self):
        api = Mock(spec=NoDeskApi)
        api.get_jobs_page.side_effect = [
            {'results': [{'hits': [{'objectID': 'old', 'datePublished': '2020-01-01'}],
                          'nbPages': 2, 'nbHits': 91, 'page': 0}]}, NoDeskFetchError('page timeout'),
        ]
        context = await self.fetch_failure(NoDeskJobsSource(NoDeskJobsSourceConfig(self.filters), api), NoDeskFetchError)
        self.assertEqual(context['page'], 1)
        self.assertIsNone(context['posting'])
        self.assertEqual(context['operation'], 'fetch_listing')

    async def test_relocate_detail_failure_identifies_url(self):
        url = 'https://relocate.me/japan/tokyo/example/engineer-123'
        api = Mock(spec=RelocateMeApi)
        api.get_jobs_page.return_value = {'urls': [url], 'next_page': None}
        api.get_job_posting.side_effect = RelocateMeFetchError('detail timeout')
        source = RelocateMeJobsSource(RelocateMeJobsSourceConfig(self.filters), api)
        context = await self.fetch_failure(source, RelocateMeFetchError)
        self.assertEqual(context['posting']['url'], url)
        self.assertEqual(context['page'], 1)
        self.assertEqual(context['operation'], 'fetch_posting')

    async def test_welcome_to_the_jungle_detail_failure_identifies_posting_and_query(self):
        source = WelcomeToTheJungleJobsSource(WelcomeToTheJungleJobsSourceConfig(SearchFilter(keywords=['flutter'])))
        record = {'reference': 'job-123', 'name': 'Engineer', 'status': 'published',
                  'published_at': CUTOFF.isoformat(), 'slug': 'engineer', 'organization': {'slug': 'example'}}
        with patch.object(source, '_request_json', side_effect=[
            {'data': [record], 'metadata': {'page': 1, 'page_count': 1}},
            WelcomeToTheJungleFetchError('detail timeout'),
        ]):
            context = await self.fetch_failure(source, WelcomeToTheJungleFetchError)
        self.assertEqual(context['posting']['reference'], 'job-123')
        self.assertEqual(context['keyword'], 'flutter')
        self.assertEqual(context['operation'], 'fetch_posting')

    async def test_wwr_feed_validation_failure_identifies_entry_and_category(self):
        source = WeWorkRemotelyJobsSource(WeWorkRemotelyJobsSourceConfig(self.filters), PythonRequestsModuleWeWorkRemotelyApi())
        content = b'''<rss><channel><item><title>Engineer</title><description>Flutter</description>
            <link>https://weworkremotely.com/remote-jobs/engineer</link><pubDate>invalid</pubDate>
            </item></channel></rss>'''
        with patch('src.job_sources.we_work_remotely.python_requests_api.requests.get',
                   return_value=Mock(content=content, status_code=200, headers={})):
            context = await self.fetch_failure(source, WeWorkRemotelyFetchError)
        self.assertEqual(context['posting']['title'], 'Engineer')
        self.assertEqual(context['category'], 'remote-customer-support-jobs')
        self.assertEqual(context['operation'], 'parse_posting')

    async def test_geekjob_parse_failure_identifies_url(self):
        url = 'https://geekjob.ru/vacancy/123'
        source = GeekJobRuJobsSource(GeekJobRuJobsSourceConfig(self.filters))
        with patch.object(source, '_collect_job_urls', return_value=[url]), \
                patch('src.job_sources.geek_job_ru.geek_job_ru.requests.get', return_value=Mock(text='<html/>')):
            context = await self.fetch_failure(source, ValueError)
        self.assertEqual(context['posting'], {'url': url})
        self.assertEqual(context['operation'], 'parse_posting')

    async def test_telegram_link_resolution_failure_identifies_channel_and_message(self):
        async def messages():
            yield Mock(id=123, date=CUTOFF, text='Flutter')
        client = Mock(iter_messages=Mock(return_value=messages()))
        source = TelegramChannelJobsSource(client, TelegramChannelJobsSourceConfig('example', self.filters, forum_id=5))
        with patch.object(source, '_get_message_url', new=AsyncMock(side_effect=RuntimeError('chat unavailable'))):
            context = await self.fetch_failure(source, RuntimeError)
        self.assertEqual(context['channel'], 'example')
        self.assertEqual(context['forum_id'], 5)
        self.assertEqual(context['posting'], {'message_id': 123})
        self.assertEqual(context['operation'], 'resolve_message_url')

    async def test_telegram_pagination_failure_does_not_identify_previous_message(self):
        async def messages():
            yield Mock(id=123, date=CUTOFF, text=None)
            raise RuntimeError('next page failed')
        source = TelegramChannelJobsSource(Mock(iter_messages=Mock(return_value=messages())),
                                           TelegramChannelJobsSourceConfig('example', self.filters))
        context = await self.fetch_failure(source, RuntimeError)
        self.assertIsNone(context['posting'])
        self.assertEqual(context['operation'], 'fetch_messages')
