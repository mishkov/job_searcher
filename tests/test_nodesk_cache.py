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

from src.job_sources.nodesk.api import NoDeskApi, NoDeskFetchError
from src.job_sources.nodesk.nodesk import NoDeskJobsSource, NoDeskJobsSourceConfig
from src.job_sources.nodesk.sql_cached_api import SqlCachedNoDeskApi
from src.search_filter import SearchFilter


URL = 'https://nodesk.co/remote-jobs/mobile-engineer/'
POSTING = {'@type': 'JobPosting', 'title': 'Mobile Engineer', 'description': '<p>Flutter development</p>'}


class NoDeskCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.cache_path = Path(self.temporary_directory.name) / 'cache root #1'
        self.database_path = self.cache_path / 'nodesk' / 'details-v1.sqlite3'
        self.api = Mock(spec=NoDeskApi)
        self.api.get_job_posting.return_value = dict(POSTING)
        self.cached_api = SqlCachedNoDeskApi(self.api, self.cache_path)
        clock_patch = patch('src.job_sources.nodesk.sql_cached_api.time.time', return_value=1000)
        self.clock = clock_patch.start()
        self.addCleanup(clock_patch.stop)

    def test_cold_cache_and_warm_cache_across_api_instances(self):
        self.assertFalse(self.cache_path.exists())
        self.assertEqual(self.cached_api.get_job_posting(URL), POSTING)
        another_api = SqlCachedNoDeskApi(self.api, self.cache_path)
        self.assertEqual(another_api.get_job_posting(URL), POSTING)
        self.api.get_job_posting.assert_called_once_with(URL)
        self.assertTrue(self.database_path.is_file())

    def test_listing_pages_always_use_upstream(self):
        self.api.get_jobs_page.side_effect = [{'results': ['old']}, {'results': ['new']}]
        self.assertEqual(self.cached_api.get_jobs_page(0, 90), {'results': ['old']})
        self.assertEqual(self.cached_api.get_jobs_page(0, 90), {'results': ['new']})
        self.assertEqual(self.api.get_jobs_page.call_count, 2)
        self.assertFalse(self.cache_path.exists())

    def test_url_changes_and_provider_queries_have_distinct_entries(self):
        for url in (URL, URL + '?version=2', "https://nodesk.co/remote-jobs/quote's-job/"):
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
        self.api.get_job_posting.side_effect = NoDeskFetchError('timed out')
        with self.assertRaisesRegex(NoDeskFetchError, 'timed out'):
            self.cached_api.get_job_posting(URL)

    def test_successful_entries_survive_a_later_upstream_failure(self):
        self.cached_api.get_job_posting(URL)
        self.api.get_job_posting.side_effect = NoDeskFetchError('timed out')
        with self.assertRaisesRegex(NoDeskFetchError, 'timed out'):
            self.cached_api.get_job_posting(URL + 'other/')
        restarted_api = SqlCachedNoDeskApi(self.api, self.cache_path)
        self.assertEqual(restarted_api.get_job_posting(URL), POSTING)
        self.assertEqual(self.api.get_job_posting.call_count, 2)

    def test_missing_jobs_and_errors_are_not_cached(self):
        self.api.get_job_posting.side_effect = [None, NoDeskFetchError('HTTP 429'), POSTING]
        self.assertIsNone(self.cached_api.get_job_posting(URL))
        self.assertFalse(self.cache_path.exists())
        with self.assertRaisesRegex(NoDeskFetchError, 'HTTP 429'):
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

        with patch('src.job_sources.nodesk.sql_cached_api.sqlite3.connect', side_effect=delete_before_read):
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

        with patch('src.job_sources.nodesk.sql_cached_api.sqlite3.connect', side_effect=delete_after_read_open):
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
        with patch('src.job_sources.nodesk.sql_cached_api.sqlite3.connect', side_effect=delete_after_open):
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
                lambda url: SqlCachedNoDeskApi(self.api, self.cache_path).get_job_posting(url), urls,
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

    def test_source_reapplies_filters_and_fresh_metadata_to_cached_details(self):
        cutoff = datetime(2026, 10, 12, tzinfo=timezone.utc)
        record = {
            'objectID': 'mobile', 'title': 'Mobile Engineer',
            'datePublished': '2026-10-12T10:00:00', 'permalink': URL,
            'company': {'name': 'Original Company'}, 'is_ad': True,
        }
        page = {'results': [{'hits': [record], 'nbHits': 1, 'nbPages': 1, 'page': 0}]}
        self.api.get_jobs_page.return_value = page
        excluded_source = NoDeskJobsSource(
            NoDeskJobsSourceConfig(SearchFilter(keywords=['python'])), self.cached_api,
        )
        self.assertEqual(asyncio.run(excluded_source.fetch(cutoff)), [])
        record['company']['name'] = 'Updated Company'
        matching_source = NoDeskJobsSource(
            NoDeskJobsSourceConfig(SearchFilter(keywords=['flutter'])),
            SqlCachedNoDeskApi(self.api, self.cache_path),
        )
        jobs = asyncio.run(matching_source.fetch(cutoff))
        self.assertEqual(len(jobs), 1)
        self.assertIn('Updated Company', jobs[0].description)
        self.api.get_job_posting.assert_called_once()
        excluded_source = NoDeskJobsSource(
            NoDeskJobsSourceConfig(SearchFilter(exclude_keywords=['flutter'])), self.cached_api,
        )
        self.assertEqual(asyncio.run(excluded_source.fetch(cutoff)), [])
        self.api.get_job_posting.assert_called_once()
        self.assertEqual(self.api.get_jobs_page.call_count, 3)

    def test_cache_keeps_successes_when_source_fetch_later_fails(self):
        records = [{
            'objectID': str(index), 'title': 'Mobile Engineer',
            'datePublished': '2026-10-12T10:00:00',
            'permalink': f'https://nodesk.co/remote-jobs/mobile-engineer-{index}/',
        } for index in range(2)]
        self.api.get_jobs_page.return_value = {'results': [{
            'hits': records, 'nbHits': 2, 'nbPages': 1, 'page': 0,
        }]}
        self.api.get_job_posting.side_effect = [dict(POSTING), NoDeskFetchError('timed out')]
        source = NoDeskJobsSource(NoDeskJobsSourceConfig(SearchFilter()), self.cached_api)
        cutoff = datetime(2026, 10, 12, tzinfo=timezone.utc)
        with self.assertRaisesRegex(NoDeskFetchError, 'timed out'):
            asyncio.run(source.fetch(cutoff))
        self.api.get_job_posting.side_effect = None
        self.api.get_job_posting.reset_mock()
        restarted_source = NoDeskJobsSource(
            NoDeskJobsSourceConfig(SearchFilter()), SqlCachedNoDeskApi(self.api, self.cache_path),
        )
        self.assertEqual(len(asyncio.run(restarted_source.fetch(cutoff))), 2)
        self.api.get_job_posting.assert_called_once_with(records[1]['permalink'])


if __name__ == '__main__':
    unittest.main()
