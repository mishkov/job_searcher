import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.job import Job
from src.job_sources.remote_ok.remote_ok import RemoteOkJobsSourceConfig
from src.job_sources.telegram_channel.telegram_channel import TelegramChannelJobsSourceConfig
from src.job_sources.web3_career.web3_career import Web3CareerJobsSourceConfig
from src.search_filter import SearchFilter
from src.storage.mysql.base import Base
from src.storage.mysql.last_search_time_model import LastSearchTimeModel
from src.storage.mysql.mysql_jobs_search_history_repository import MysqlJobsSearchHistoryRespository


class SourceIdentityTests(unittest.TestCase):
    def test_identity_is_stable_across_instances_order_case_and_display_names(self):
        first = RemoteOkJobsSourceConfig(SearchFilter(['Flutter', 'python', 'flutter'], ['Senior', 'Junior']))
        second = RemoteOkJobsSourceConfig(SearchFilter(['PYTHON', 'flutter'], ['junior', 'senior']))
        object.__setattr__(second, 'name', 'New display name')
        self.assertEqual(first.history_key, second.history_key)
        self.assertEqual(len(first.history_key), 64)

    def test_provider_and_filter_changes_get_independent_checkpoints(self):
        configs = [
            RemoteOkJobsSourceConfig(SearchFilter(['flutter'])),
            RemoteOkJobsSourceConfig(SearchFilter(['python'])),
            RemoteOkJobsSourceConfig(SearchFilter(['flutter'], ['senior'])),
            Web3CareerJobsSourceConfig(SearchFilter(['flutter'])),
        ]
        self.assertEqual(len({config.history_key for config in configs}), len(configs))

    def test_credentials_do_not_change_identity(self):
        first = Web3CareerJobsSourceConfig(SearchFilter(), api_token='first-token')
        second = Web3CareerJobsSourceConfig(SearchFilter(), api_token='second-token')
        self.assertEqual(first.history_key, second.history_key)

    def test_telegram_channels_and_topics_are_independent(self):
        filters = SearchFilter(['flutter'])
        configs = [TelegramChannelJobsSourceConfig(entity, filters, topic)
                   for entity, topic in [('jobs', None), ('other', None), ('jobs', 1), ('jobs', 2), (-100123, 1)]]
        self.assertEqual(len({config.history_key for config in configs}), len(configs))
        equivalent = TelegramChannelJobsSourceConfig('@JOBS', filters)
        self.assertEqual(configs[0].history_key, equivalent.history_key)


class SourceHistoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://')
        Base.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        self.started_at = datetime(2026, 10, 14, 10, 0, tzinfo=timezone.utc)

    async def test_new_sources_ignore_legacy_timestamp_and_preserve_seen_jobs(self):
        job = Job('Title', 'Description', '-', None, 'https://example.com', 'fingerprint')
        with Session(self.engine) as session:
            session.add(LastSearchTimeModel(id=1, last_searched_at=self.started_at.replace(tzinfo=None)))
            session.commit()
            repository = MysqlJobsSearchHistoryRespository(session)
            await repository.add_seen_job(job)
            self.assertIsNone(await repository.get_last_search_time('new-source'))
            await repository.save_last_search_time('new-source', self.started_at)
        with Session(self.engine) as session:
            repository = MysqlJobsSearchHistoryRespository(session)
            self.assertTrue(await repository.seen(job))
            self.assertIsNotNone(session.get(LastSearchTimeModel, 1))

    async def test_checkpoints_persist_independently_across_repository_instances(self):
        later = self.started_at + timedelta(hours=1)
        with Session(self.engine) as session:
            repository = MysqlJobsSearchHistoryRespository(session)
            await repository.save_last_search_time('source-a', self.started_at)
            await repository.save_last_search_time('source-b', later)
            await repository.save_last_search_time('source-a', later + timedelta(hours=1))
        with Session(self.engine) as session:
            repository = MysqlJobsSearchHistoryRespository(session)
            self.assertEqual(await repository.get_last_search_time('source-a'), later + timedelta(hours=1))
            self.assertEqual(await repository.get_last_search_time('source-b'), later)
            self.assertIsNone(await repository.get_last_search_time('source-c'))

    async def test_timestamps_round_trip_as_aware_utc(self):
        with Session(self.engine) as session:
            repository = MysqlJobsSearchHistoryRespository(session)
            local_time = self.started_at.astimezone(timezone(timedelta(hours=3)))
            await repository.save_last_search_time('source', local_time)
        with Session(self.engine) as session:
            result = await MysqlJobsSearchHistoryRespository(session).get_last_search_time('source')
            self.assertEqual(result, self.started_at)
            self.assertEqual(result.tzinfo, timezone.utc)
