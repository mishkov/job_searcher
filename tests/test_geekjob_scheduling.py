import asyncio
import threading
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from src.job_sources.geek_job_ru.geek_job_ru import GeekJobRuJobsSource, GeekJobRuJobsSourceConfig
from src.search_filter import SearchFilter


class GeekJobSchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def test_listing_advances_page_and_sets_request_timeout(self):
        source = GeekJobRuJobsSource(GeekJobRuJobsSourceConfig(SearchFilter()))
        responses = [Mock(json=Mock(return_value={'nextpage': 2, 'data': []})),
                     Mock(json=Mock(return_value={'nextpage': 0, 'data': []}))]
        with patch('src.job_sources.geek_job_ru.geek_job_ru.requests.get', side_effect=responses) as get:
            self.assertEqual(await source.fetch(datetime.now(timezone.utc)), [])
        self.assertEqual(get.call_count, 2)
        self.assertIn('page=1', get.call_args_list[0].args[0])
        self.assertIn('page=2', get.call_args_list[1].args[0])
        self.assertTrue(all(call.kwargs['timeout'] == 15 for call in get.call_args_list))

    async def test_fetch_does_not_block_commands(self):
        entered, release = threading.Event(), threading.Event()
        source = GeekJobRuJobsSource(GeekJobRuJobsSourceConfig(SearchFilter()))
        def blocking_fetch(since):
            entered.set()
            release.wait(timeout=2)
            return []
        with patch.object(source, '_fetch', side_effect=blocking_fetch):
            task = asyncio.create_task(source.fetch(datetime.now(timezone.utc)))
            try:
                await asyncio.wait_for(asyncio.to_thread(entered.wait, 1), 1.5)
                self.assertTrue(entered.is_set())
                self.assertFalse(task.done())
            finally:
                release.set()
                await task
