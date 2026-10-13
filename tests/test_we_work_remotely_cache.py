import asyncio
import json
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from src.job_sources.we_work_remotely.api import (
    CATEGORIES, FeedResponse, WeWorkRemotelyApi, WeWorkRemotelyFetchError,
)
from src.job_sources.we_work_remotely.sql_cached_api import SqlCachedWeWorkRemotelyApi
from src.job_sources.we_work_remotely.we_work_remotely import (
    WeWorkRemotelyJobsSource, WeWorkRemotelyJobsSourceConfig,
)
from src.search_filter import SearchFilter
from test_we_work_remotely import CUTOFF, vacancy


CATEGORY = CATEGORIES[0]


class WeWorkRemotelyCacheTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.cache_path = Path(directory.name) / 'cache root #1'
        self.database_path = self.cache_path / 'we_work_remotely' / 'feeds-v1.sqlite3'
        self.api = Mock(spec=WeWorkRemotelyApi)
        self.api.get_feed.return_value = FeedResponse([vacancy()], 'W/"one"')
        self.cache = SqlCachedWeWorkRemotelyApi(self.api, self.cache_path)
        clock_patch = patch('src.job_sources.we_work_remotely.sql_cached_api.time.time', return_value=1000)
        self.clock = clock_patch.start()
        self.addCleanup(clock_patch.stop)

    def warm(self):
        self.cache.get_feed(CATEGORY)
        self.api.reset_mock()
        self.api.get_feed.return_value = FeedResponse(None)

    def test_cold_and_warm_cache_across_instances_always_revalidate(self):
        self.assertFalse(self.cache_path.exists())
        self.assertEqual(self.cache.get_feed(CATEGORY).jobs, [vacancy()])
        self.api.get_feed.assert_called_once_with(CATEGORY, etag=None, last_modified=None)
        self.api.reset_mock()
        self.api.get_feed.return_value = FeedResponse(None)
        other = SqlCachedWeWorkRemotelyApi(self.api, self.cache_path)
        self.assertEqual(other.get_feed(CATEGORY).jobs, [vacancy()])
        self.api.get_feed.assert_called_once_with(CATEGORY, etag='W/"one"', last_modified=None)

    def test_new_changed_removed_and_empty_feeds_replace_snapshot(self):
        self.warm()
        updated = vacancy(description='<p>Dart only now</p>')
        self.api.get_feed.return_value = FeedResponse([updated], 'W/"two"')
        self.assertEqual(self.cache.get_feed(CATEGORY).jobs, [updated])
        self.api.get_feed.return_value = FeedResponse(None)
        self.assertEqual(self.cache.get_feed(CATEGORY).jobs, [updated])
        self.api.get_feed.return_value = FeedResponse([], 'W/"empty"')
        self.assertEqual(self.cache.get_feed(CATEGORY).jobs, [])
        self.api.get_feed.return_value = FeedResponse(None)
        self.assertEqual(self.cache.get_feed(CATEGORY).jobs, [])

    def test_last_modified_only_and_validator_updates(self):
        date = 'Mon, 12 Oct 2026 10:00:00 GMT'
        self.api.get_feed.return_value = FeedResponse([vacancy()], last_modified=date)
        self.cache.get_feed(CATEGORY)
        self.api.get_feed.return_value = FeedResponse(None, etag='W/"new"')
        self.cache.get_feed(CATEGORY)
        self.api.get_feed.assert_called_with(CATEGORY, etag=None, last_modified=date)
        self.cache.get_feed(CATEGORY)
        self.api.get_feed.assert_called_with(CATEGORY, etag='W/"new"', last_modified=date)

    def test_no_validators_means_unconditional_requests_without_cache_writes(self):
        self.api.get_feed.return_value = FeedResponse([vacancy()])
        for _ in range(2):
            self.assertEqual(self.cache.get_feed(CATEGORY).jobs, [vacancy()])
        self.assertFalse(self.cache_path.exists())
        self.api.get_feed.assert_called_with(CATEGORY, etag=None, last_modified=None)

    def test_expiry_and_clock_rollback_refetch_unconditionally_and_prune(self):
        for clock in (999, 1000 + 86400):
            with self.subTest(clock=clock):
                self.clock.return_value = 1000
                self.api.get_feed.return_value = FeedResponse([vacancy()], 'one')
                self.cache.get_feed(CATEGORY)
                self.clock.return_value = clock
                self.cache.get_feed(CATEGORY)
                self.api.get_feed.assert_called_with(CATEGORY, etag=None, last_modified=None)
        self.clock.return_value += 86400
        self.cache.get_feed(CATEGORIES[1])
        with closing(sqlite3.connect(self.database_path)) as database:
            self.assertEqual(database.execute('SELECT category FROM feeds').fetchall(), [(CATEGORIES[1],)])

    def test_upstream_failures_never_serve_cache_and_keep_prior_successes(self):
        self.warm()
        self.api.get_feed.side_effect = WeWorkRemotelyFetchError('timeout')
        for clock in (1000, 1000 + 86400):
            self.clock.return_value = clock
            with self.assertRaisesRegex(WeWorkRemotelyFetchError, 'timeout'):
                self.cache.get_feed(CATEGORY)
        self.clock.return_value = 1000
        self.api.get_feed.side_effect = None
        self.api.get_feed.return_value = FeedResponse(None)
        self.assertEqual(self.cache.get_feed(CATEGORY).jobs, [vacancy()])

    def test_unsolicited_304_without_cache_is_an_error(self):
        self.api.get_feed.return_value = FeedResponse(None)
        with self.assertRaisesRegex(WeWorkRemotelyFetchError, 'unexpected HTTP 304'):
            self.cache.get_feed(CATEGORY)
        self.assertFalse(self.cache_path.exists())

    def test_deleted_cache_between_calls_and_during_fetch_is_recreated(self):
        self.cache.get_feed(CATEGORY)
        shutil.rmtree(self.cache_path)
        self.cache.get_feed(CATEGORY)
        self.assertTrue(self.database_path.is_file())
        self.api.get_feed.side_effect = lambda *args, **kwargs: (
            shutil.rmtree(self.cache_path) or FeedResponse([vacancy()], 'two')
        )
        self.cache.get_feed(CATEGORY)
        self.assertTrue(self.database_path.is_file())

    def test_deletion_races_during_read_and_write_preserve_successful_response(self):
        for stage in ('before_read', 'after_read', 'after_write'):
            with self.subTest(stage=stage):
                self.api.get_feed.return_value = FeedResponse([vacancy()], 'one')
                self.cache.get_feed(CATEGORY)
                connect = sqlite3.connect

                def deleting_connect(*args, **kwargs):
                    reading = kwargs.get('uri', False)
                    if stage == 'before_read' and reading:
                        shutil.rmtree(self.cache_path)
                    database = connect(*args, **kwargs)
                    if stage == 'after_read' and reading or stage == 'after_write' and not reading:
                        shutil.rmtree(self.cache_path)
                    return database

                with patch('src.job_sources.we_work_remotely.sql_cached_api.sqlite3.connect',
                           side_effect=deleting_connect):
                    self.assertEqual(self.cache.get_feed(CATEGORY).jobs, [vacancy()])
                self.cache.get_feed(CATEGORY)
                self.assertTrue(self.database_path.is_file())

    def test_corrupt_database_and_unwritable_directory_are_optional_and_not_deleted(self):
        self.database_path.parent.mkdir(parents=True)
        self.database_path.write_bytes(b'user data')
        self.assertEqual(self.cache.get_feed(CATEGORY).jobs, [vacancy()])
        self.api.get_feed.assert_called_with(CATEGORY, etag=None, last_modified=None)
        self.assertEqual(self.database_path.read_bytes(), b'user data')
        shutil.rmtree(self.cache_path)
        with patch.object(Path, 'mkdir', side_effect=PermissionError('read only')):
            self.assertEqual(self.cache.get_feed(CATEGORY).jobs, [vacancy()])
        self.cache_path.write_text('keep this file')
        self.assertEqual(self.cache.get_feed(CATEGORY).jobs, [vacancy()])
        self.assertEqual(self.cache_path.read_text(), 'keep this file')

    def test_corrupt_payload_dates_urls_and_header_injection_are_cache_misses(self):
        bad = [dict(jobs=[vacancy(pubDate='bad')], etag='one', last_modified=None),
               dict(jobs=[vacancy(link='https://evil.example/job')], etag='one', last_modified=None),
               dict(jobs=[vacancy()], etag='one\r\nInjected: yes', last_modified=None)]
        for payload in ('invalid JSON', '[]', '{}', *[json.dumps(data) for data in bad]):
            self.cache.get_feed(CATEGORY)
            with closing(sqlite3.connect(self.database_path)) as database:
                database.execute('UPDATE feeds SET payload = ?', (payload,))
                database.commit()
            self.api.reset_mock()
            self.assertEqual(self.cache.get_feed(CATEGORY).jobs, [vacancy()])
            self.api.get_feed.assert_called_once_with(CATEGORY, etag=None, last_modified=None)

    def test_database_lock_does_not_block_search(self):
        self.cache.get_feed(CATEGORY)
        with closing(sqlite3.connect(self.database_path)) as database:
            database.execute('BEGIN EXCLUSIVE')
            try:
                self.assertEqual(self.cache.get_feed(CATEGORY).jobs, [vacancy()])
                self.api.get_feed.assert_called_with(CATEGORY, etag=None, last_modified=None)
            finally:
                database.rollback()

    def test_filters_dates_and_expiry_are_applied_after_cache_revalidation(self):
        self.api.get_feed.side_effect = lambda category, **kwargs: (
            FeedResponse(None) if kwargs['etag'] else
            FeedResponse([vacancy()] if category == CATEGORY else [], 'one')
        )
        clock_patch = patch('src.job_sources.we_work_remotely.we_work_remotely.datetime', wraps=datetime)
        with clock_patch as clock:
            clock.now.return_value = CUTOFF
            def search(filters, cutoff=CUTOFF):
                source = WeWorkRemotelyJobsSource(
                    WeWorkRemotelyJobsSourceConfig(filters),
                    SqlCachedWeWorkRemotelyApi(self.api, self.cache_path),
                )
                return asyncio.run(source.fetch(cutoff))
            self.assertEqual(search(SearchFilter(keywords=['python'])), [])
            self.assertEqual(len(search(SearchFilter(keywords=['flutter']))), 1)
            self.assertEqual(search(SearchFilter(exclude_keywords=['flutter'])), [])
            self.assertEqual(search(SearchFilter(), CUTOFF.replace(year=2027)), [])
            clock.now.return_value = CUTOFF.replace(year=2027)
            self.assertEqual(search(SearchFilter()), [])
        self.assertEqual(self.api.get_feed.call_count, 50)

    def test_successful_category_is_persisted_when_next_category_fails(self):
        self.api.get_feed.side_effect = [FeedResponse([vacancy()], 'one'), WeWorkRemotelyFetchError('timeout')]
        source = WeWorkRemotelyJobsSource(WeWorkRemotelyJobsSourceConfig(SearchFilter()), self.cache)
        with self.assertRaisesRegex(WeWorkRemotelyFetchError, 'timeout'):
            asyncio.run(source.fetch(CUTOFF))
        self.api.get_feed.side_effect = None
        self.api.get_feed.return_value = FeedResponse(None)
        self.assertEqual(SqlCachedWeWorkRemotelyApi(self.api, self.cache_path).get_feed(CATEGORY).jobs, [vacancy()])
