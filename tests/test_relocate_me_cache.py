import asyncio
import shutil
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from src.job_sources.relocate_me.api import RelocateMeApi, RelocateMeFetchError
from src.job_sources.relocate_me.relocate_me import RelocateMeJobsSource, RelocateMeJobsSourceConfig
from src.job_sources.relocate_me.sql_cached_api import SqlCachedRelocateMeApi
from src.search_filter import SearchFilter


URL = 'https://relocate.me/japan/tokyo/example/mobile-engineer-12345'
POSTING = {'id': '12345', 'title': 'Mobile Engineer', 'description': 'Flutter development',
           'date_posted': '2026-10-12', 'salary': '-', 'remote': None}


class RelocateMeCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.cache_path = Path(self.temporary_directory.name) / 'cache root #1'
        self.database_path = self.cache_path / 'relocate_me' / 'details-v1.sqlite3'
        self.api = Mock(spec=RelocateMeApi)
        self.api.get_job_posting.return_value = dict(POSTING)
        self.cached_api = SqlCachedRelocateMeApi(self.api, self.cache_path)
        clock_patch = patch('src.job_sources.relocate_me.sql_cached_api.time.time', return_value=1000)
        self.clock = clock_patch.start()
        self.addCleanup(clock_patch.stop)

    def test_cold_cache_and_warm_cache_across_api_instances(self):
        self.assertFalse(self.cache_path.exists())
        self.assertEqual(self.cached_api.get_job_posting(URL), POSTING)
        another_api = SqlCachedRelocateMeApi(self.api, self.cache_path)
        self.assertEqual(another_api.get_job_posting(URL), POSTING)
        self.api.get_job_posting.assert_called_once_with(URL)
        self.assertTrue(self.database_path.is_file())

    def test_listing_pages_always_use_upstream(self):
        self.api.get_jobs_page.side_effect = [{'results': ['old']}, {'results': ['new']}]
        self.assertEqual(self.cached_api.get_jobs_page(1), {'results': ['old']})
        self.assertEqual(self.cached_api.get_jobs_page(1), {'results': ['new']})
        self.assertEqual(self.api.get_jobs_page.call_count, 2)
        self.assertFalse(self.cache_path.exists())

    def test_url_changes_and_provider_queries_have_distinct_entries(self):
        for url in (URL, URL + '?version=2', "https://relocate.me/japan/tokyo/example/quote's-job-12345"):
            self.assertEqual(self.cached_api.get_job_posting(url), POSTING)
            self.assertEqual(self.cached_api.get_job_posting(url), POSTING)
        self.assertEqual(self.api.get_job_posting.call_count, 3)

    def test_entries_expire_after_24_hours_and_refresh(self):
        self.cached_api.get_job_posting(URL)
        self.clock.return_value = 1000 + 86400 - 1
        self.assertEqual(self.cached_api.get_job_posting(URL), POSTING)
        self.api.get_job_posting.assert_called_once()
        self.clock.return_value += 1
        fresh = {**POSTING, 'description': 'Updated description'}
        self.api.get_job_posting.return_value = fresh
        self.assertEqual(self.cached_api.get_job_posting(URL), fresh)
        self.assertEqual(self.cached_api.get_job_posting(URL), fresh)
        self.assertEqual(self.api.get_job_posting.call_count, 2)

    def test_expired_cache_does_not_hide_upstream_failure(self):
        self.cached_api.get_job_posting(URL)
        self.clock.return_value += 86400
        self.api.get_job_posting.side_effect = RelocateMeFetchError('timed out')
        with self.assertRaisesRegex(RelocateMeFetchError, 'timed out'):
            self.cached_api.get_job_posting(URL)

    def test_successful_entries_survive_a_later_upstream_failure(self):
        self.cached_api.get_job_posting(URL)
        self.api.get_job_posting.side_effect = RelocateMeFetchError('timed out')
        with self.assertRaisesRegex(RelocateMeFetchError, 'timed out'):
            self.cached_api.get_job_posting(URL + 'other/')
        restarted_api = SqlCachedRelocateMeApi(self.api, self.cache_path)
        self.assertEqual(restarted_api.get_job_posting(URL), POSTING)
        self.assertEqual(self.api.get_job_posting.call_count, 2)

    def test_missing_jobs_and_errors_are_not_cached(self):
        self.api.get_job_posting.side_effect = [None, RelocateMeFetchError('HTTP 429'), POSTING]
        self.assertIsNone(self.cached_api.get_job_posting(URL))
        self.assertFalse(self.cache_path.exists())
        with self.assertRaisesRegex(RelocateMeFetchError, 'HTTP 429'):
            self.cached_api.get_job_posting(URL)
        self.assertEqual(self.cached_api.get_job_posting(URL), POSTING)
        self.assertEqual(self.api.get_job_posting.call_count, 3)

    def test_cache_directory_can_be_deleted_between_calls(self):
        self.cached_api.get_job_posting(URL)
        shutil.rmtree(self.cache_path)
        self.assertEqual(self.cached_api.get_job_posting(URL), POSTING)
        self.assertEqual(self.cached_api.get_job_posting(URL), POSTING)
        self.assertTrue(self.database_path.is_file())
        self.assertEqual(self.api.get_job_posting.call_count, 2)

    def test_database_can_be_deleted_without_deleting_its_directory(self):
        self.cached_api.get_job_posting(URL)
        self.database_path.unlink()
        self.assertEqual(self.cached_api.get_job_posting(URL), POSTING)
        self.assertEqual(self.api.get_job_posting.call_count, 2)

    def test_deletion_between_file_check_and_read_is_a_cache_miss(self):
        self.cached_api.get_job_posting(URL)
        connect = sqlite3.connect

        def delete_before_read(*args, **kwargs):
            if kwargs.get('uri'):
                shutil.rmtree(self.cache_path)
            return connect(*args, **kwargs)

        with patch('src.job_sources.relocate_me.sql_cached_api.sqlite3.connect', side_effect=delete_before_read):
            self.assertEqual(self.cached_api.get_job_posting(URL), POSTING)
        self.assertEqual(self.api.get_job_posting.call_count, 2)
        self.assertTrue(self.database_path.is_file())

    def test_deletion_during_upstream_fetch_recreates_cache_for_write(self):
        self.cached_api.get_job_posting(URL)

        def fetch_and_delete(url):
            shutil.rmtree(self.cache_path)
            return dict(POSTING)

        self.api.get_job_posting.side_effect = fetch_and_delete
        new_url = URL + 'other/'
        self.assertEqual(self.cached_api.get_job_posting(new_url), POSTING)
        self.assertEqual(self.cached_api.get_job_posting(new_url), POSTING)
        self.assertTrue(self.database_path.is_file())
        self.assertEqual(self.api.get_job_posting.call_count, 2)

    def test_deletion_after_read_connection_opens_is_safe(self):
        self.cached_api.get_job_posting(URL)
        connect = sqlite3.connect

        def delete_after_read_open(*args, **kwargs):
            database = connect(*args, **kwargs)
            if kwargs.get('uri'):
                shutil.rmtree(self.cache_path)
            return database

        with patch('src.job_sources.relocate_me.sql_cached_api.sqlite3.connect', side_effect=delete_after_read_open):
            self.assertEqual(self.cached_api.get_job_posting(URL), POSTING)
        self.assertEqual(self.cached_api.get_job_posting(URL), POSTING)
        self.assertTrue(self.database_path.is_file())

    def test_deletion_after_write_connection_opens_does_not_lose_api_response(self):
        self.cached_api.get_job_posting(URL)
        connect = sqlite3.connect

        def delete_after_open(*args, **kwargs):
            database = connect(*args, **kwargs)
            if not kwargs.get('uri'):
                shutil.rmtree(self.cache_path)
            return database

        new_url = URL + 'other/'
        with patch('src.job_sources.relocate_me.sql_cached_api.sqlite3.connect', side_effect=delete_after_open):
            self.assertEqual(self.cached_api.get_job_posting(new_url), POSTING)
        self.assertEqual(self.cached_api.get_job_posting(new_url), POSTING)
        self.assertTrue(self.database_path.is_file())

    def test_unwritable_cache_and_file_in_place_of_directory_are_optional(self):
        with patch.object(Path, 'mkdir', side_effect=PermissionError('read-only')):
            self.assertEqual(self.cached_api.get_job_posting(URL), POSTING)
        self.cache_path.write_text('User data must not be removed')
        self.assertEqual(self.cached_api.get_job_posting(URL), POSTING)
        self.assertEqual(self.cache_path.read_text(), 'User data must not be removed')
        self.assertEqual(self.api.get_job_posting.call_count, 2)

    def test_corrupt_database_does_not_fail_search_or_delete_files(self):
        self.database_path.parent.mkdir(parents=True)
        self.database_path.write_bytes(b'Not a SQLite database')
        self.assertEqual(self.cached_api.get_job_posting(URL), POSTING)
        self.assertEqual(self.cached_api.get_job_posting(URL), POSTING)
        self.assertEqual(self.database_path.read_bytes(), b'Not a SQLite database')
        self.assertEqual(self.api.get_job_posting.call_count, 2)

    def test_invalid_cached_payload_is_refetched_and_replaced(self):
        for payload in ('not JSON', '[]', '{}', '{"description":123}'):
            with self.subTest(payload=payload):
                self.cached_api.get_job_posting(URL)
                with closing(sqlite3.connect(self.database_path)) as database:
                    database.execute('UPDATE job_details SET payload = ? WHERE url = ?', (payload, URL))
                    database.commit()
                self.api.get_job_posting.reset_mock()
                self.assertEqual(self.cached_api.get_job_posting(URL), POSTING)
                self.assertEqual(self.cached_api.get_job_posting(URL), POSTING)
                self.api.get_job_posting.assert_called_once()

    def test_clock_rollback_invalidates_future_cache_entry(self):
        self.cached_api.get_job_posting(URL)
        self.clock.return_value = 999
        self.assertEqual(self.cached_api.get_job_posting(URL), POSTING)
        self.assertEqual(self.api.get_job_posting.call_count, 2)

    def test_expired_entries_are_pruned_on_successful_writes(self):
        self.cached_api.get_job_posting(URL)
        self.clock.return_value += 86400
        self.cached_api.get_job_posting(URL + 'other/')
        with closing(sqlite3.connect(self.database_path)) as database:
            urls = database.execute('SELECT url FROM job_details').fetchall()
        self.assertEqual(urls, [(URL + 'other/',)])

    def test_locked_database_does_not_fail_search(self):
        self.cached_api.get_job_posting(URL)
        database = sqlite3.connect(self.database_path)
        self.addCleanup(database.close)
        database.execute('BEGIN EXCLUSIVE')
        try:
            self.assertEqual(self.cached_api.get_job_posting(URL), POSTING)
        finally:
            database.rollback()
        self.assertEqual(self.api.get_job_posting.call_count, 2)
        self.assertEqual(self.cached_api.get_job_posting(URL), POSTING)

    def test_concurrent_api_instances_keep_valid_entries(self):
        # Warm up the schema; each worker then creates its own connections.
        self.cached_api.get_job_posting(URL)
        urls = [URL + str(index) for index in range(4)]
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(
                lambda url: SqlCachedRelocateMeApi(self.api, self.cache_path).get_job_posting(url), urls,
            ))
        self.assertEqual(results, [POSTING] * 4)
        self.api.get_job_posting.reset_mock()
        for url in urls:
            self.assertEqual(self.cached_api.get_job_posting(url), POSTING)
        # A contended write may be skipped; all returned and stored data stays valid.

    def test_callers_cannot_mutate_the_persisted_response(self):
        self.cached_api.get_job_posting(URL)['description'] = 'Mutated'
        self.assertEqual(self.cached_api.get_job_posting(URL), POSTING)

    def test_non_serializable_response_does_not_make_cache_required(self):
        posting = {**POSTING, 'unexpected': object()}
        self.api.get_job_posting.return_value = posting
        self.assertIs(self.cached_api.get_job_posting(URL), posting)

    def test_source_reapplies_keyword_exclusion_and_date_filters_to_cached_details(self):
        cutoff = datetime(2026, 10, 12, 10, tzinfo=timezone.utc)
        self.api.get_jobs_page.return_value = {'urls': [URL], 'next_page': None}
        for filters, since, expected in (
            (SearchFilter(keywords=['python']), cutoff, 0),
            (SearchFilter(keywords=['flutter']), cutoff, 1),
            (SearchFilter(exclude_keywords=['flutter']), cutoff, 0),
            (SearchFilter(), datetime(2026, 10, 13, tzinfo=timezone.utc), 0),
        ):
            source = RelocateMeJobsSource(
                RelocateMeJobsSourceConfig(filters), SqlCachedRelocateMeApi(self.api, self.cache_path),
            )
            self.assertEqual(len(asyncio.run(source.fetch(since))), expected)
        self.api.get_job_posting.assert_called_once_with(URL)
        self.assertEqual(self.api.get_jobs_page.call_count, 4)

    def test_warm_cache_does_not_resurrect_removed_listings_and_discovers_new_jobs(self):
        cutoff = datetime(2026, 10, 12, tzinfo=timezone.utc)
        other_url = 'https://relocate.me/japan/tokyo/example/new-engineer-67890'
        self.api.get_jobs_page.side_effect = [
            {'urls': [URL], 'next_page': None},
            {'urls': [other_url], 'next_page': None},
        ]
        self.api.get_job_posting.side_effect = [dict(POSTING), {**POSTING, 'id': '67890'}]
        source = RelocateMeJobsSource(RelocateMeJobsSourceConfig(SearchFilter()), self.cached_api)
        self.assertEqual([job.url for job in asyncio.run(source.fetch(cutoff))], [URL])
        self.assertEqual([job.url for job in asyncio.run(source.fetch(cutoff))], [other_url])

    def test_cached_expiry_is_rechecked_on_every_search(self):
        self.api.get_job_posting.return_value = {**POSTING, 'valid_through': '2026-10-12T12:00:00Z'}
        self.api.get_jobs_page.return_value = {'urls': [URL], 'next_page': None}
        cutoff = datetime(2026, 10, 12, tzinfo=timezone.utc)
        source = RelocateMeJobsSource(RelocateMeJobsSourceConfig(SearchFilter()), self.cached_api)
        with patch('src.job_sources.relocate_me.api.datetime') as clock:
            clock.fromisoformat.side_effect = datetime.fromisoformat
            clock.now.return_value = datetime(2026, 10, 12, 11, tzinfo=timezone.utc)
            self.assertEqual(len(asyncio.run(source.fetch(cutoff))), 1)
            clock.now.return_value = datetime(2026, 10, 12, 13, tzinfo=timezone.utc)
            self.assertEqual(asyncio.run(source.fetch(cutoff)), [])
        self.api.get_job_posting.assert_called_once_with(URL)

    def test_details_cached_before_a_later_source_failure_are_reused(self):
        cutoff = datetime(2026, 10, 12, tzinfo=timezone.utc)
        other_url = 'https://relocate.me/japan/tokyo/example/new-engineer-67890'
        self.api.get_jobs_page.return_value = {'urls': [URL, other_url], 'next_page': None}
        self.api.get_job_posting.side_effect = [dict(POSTING), RelocateMeFetchError('timed out')]
        source = RelocateMeJobsSource(RelocateMeJobsSourceConfig(SearchFilter()), self.cached_api)
        with self.assertRaisesRegex(RelocateMeFetchError, 'timed out'):
            asyncio.run(source.fetch(cutoff))
        self.api.get_job_posting.side_effect = None
        self.api.get_job_posting.return_value = {**POSTING, 'id': '67890'}
        self.api.get_job_posting.reset_mock()
        restarted = RelocateMeJobsSource(
            RelocateMeJobsSourceConfig(SearchFilter()), SqlCachedRelocateMeApi(self.api, self.cache_path),
        )
        self.assertEqual(len(asyncio.run(restarted.fetch(cutoff))), 2)
        self.api.get_job_posting.assert_called_once_with(other_url)


if __name__ == '__main__':
    unittest.main()
