import asyncio
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from sqlalchemy import create_engine, text

from src.job import Job
from src.monitoring import LOCAL_TIMEZONE, MonitoringState, SearchResult, SleepPeriod, VacancyMonitor
from src.storage.mysql.base import Base
from src.storage.mysql.monitoring_store import MonitoringModel, MysqlMonitoringStore
from src.storage.mysql.threaded_history_repository import ThreadedHistoryRepository


def local(value):
    return datetime.fromisoformat(value).replace(tzinfo=LOCAL_TIMEZONE).astimezone(timezone.utc)


class SleepPeriodTests(unittest.TestCase):
    def test_format_and_validation(self):
        self.assertEqual(str(SleepPeriod.parse(' 23:00 - 08:00 ')), '23:00-08:00')
        for value in ('23 00-08 00', '24:00-08:00', '23:60-08:00', '8:00-9:00',
                      '08:00-08:00', '23:00-08:00, 12:00-13:00', 'garbage', ''):
            with self.subTest(value=value), self.assertRaises(ValueError):
                SleepPeriod.parse(value)

    def test_overnight_boundaries_in_minsk(self):
        period = SleepPeriod.parse('23:00-08:00')
        for value, expected in [
            ('2026-10-22T22:59:59', None),
            ('2026-10-22T23:00:00', '2026-10-23T08:00'),
            ('2026-10-23T00:00:00', '2026-10-23T08:00'),
            ('2026-10-23T07:59:59', '2026-10-23T08:00'),
            ('2026-10-23T08:00:00', None),
        ]:
            with self.subTest(value=value):
                self.assertEqual(period.sleeping_until(local(value)), local(expected) if expected else None)

    def test_daytime_and_midnight_end(self):
        for period, now, end in [
            ('13:00-14:00', '2026-10-22T13:00', '2026-10-22T14:00'),
            ('23:00-00:00', '2026-10-22T23:30', '2026-10-23T00:00'),
            ('00:00-08:00', '2026-10-22T00:00', '2026-10-22T08:00'),
        ]:
            with self.subTest(period=period):
                self.assertEqual(SleepPeriod.parse(period).sleeping_until(local(now)), local(end))

    def test_wake_up_overrides_long_interval(self):
        period = SleepPeriod.parse('23:00-08:00')
        now = local('2026-10-22T22:50')
        for due in ('2026-10-22T23:00', '2026-10-23T00:30', '2026-10-23T10:00'):
            self.assertEqual(period.next_check(now, local(due)), local('2026-10-23T08:00'))
        self.assertEqual(period.next_check(now, now + timedelta(minutes=5)), now + timedelta(minutes=5))


class MemoryStore:
    def __init__(self, state=None):
        self.state = state or MonitoringState()

    async def load(self):
        return self.state

    async def save(self, state):
        self.state = state


class MonitorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = local('2026-10-22T12:00')
        self.store = MemoryStore()
        self.search = AsyncMock(return_value=SearchResult())
        self.reporter = Mock()
        self.monitor = VacancyMonitor(self.store, self.store.state, 30, self.search,
                                      self.reporter, now=lambda: self.now)

    async def test_start_is_idempotent_and_stop_survives_restart(self):
        self.assertIsNone(self.monitor.next_check())
        self.assertTrue(await self.monitor.start(12345))
        self.assertEqual(self.monitor.next_check(), self.now)
        await self.monitor.check_once()
        self.assertEqual(self.monitor.next_check(), self.now + timedelta(minutes=30))
        self.assertFalse(await self.monitor.start(12345))
        self.assertEqual(self.monitor.next_check(), self.now + timedelta(minutes=30))
        await self.monitor.stop()
        restarted = VacancyMonitor(self.store, await self.store.load(), 30, self.search, self.reporter)
        self.assertFalse(restarted.can_search())
        self.assertIsNone(restarted.next_check())
        self.assertEqual(restarted.state.chat_id, 12345)

    async def test_saved_enabled_state_waits_for_start_if_delivery_chat_is_unknown(self):
        self.monitor.state = MonitoringState(enabled=True)
        self.assertFalse(self.monitor.can_search())
        self.assertIsNone(self.monitor.next_check())
        self.assertIn('Send /start', self.monitor.status())
        await self.monitor.check_once()
        self.search.assert_not_awaited()
        self.assertTrue(await self.monitor.start(12345))
        self.assertEqual(self.monitor.state.chat_id, 12345)
        self.assertTrue(self.monitor.can_search())

    async def test_interval_starts_after_completion(self):
        async def search(*args):
            self.now += timedelta(minutes=12)
            return SearchResult(delivered=2, failed_sources=1)
        self.search.side_effect = search
        await self.monitor.start(12345)
        await self.monitor.check_once()
        self.assertEqual(self.monitor.state.last_started_at, local('2026-10-22T12:00'))
        self.assertEqual(self.monitor.state.last_completed_at, local('2026-10-22T12:12'))
        self.assertEqual(self.monitor.next_check(), local('2026-10-22T12:42'))
        self.assertIn('2 new vacancies; 1 failed sources', self.monitor.status())

    async def test_failed_cycle_is_reported_once_and_retried(self):
        self.search.side_effect = [RuntimeError('private error details'), SearchResult()]
        await self.monitor.start(12345)
        await self.monitor.check_once()
        self.assertIn('RuntimeError', self.monitor.status())
        self.assertNotIn('private error details', self.monitor.status())
        self.reporter.report.assert_called_once()
        self.assertFalse(self.monitor.running)
        self.now += timedelta(minutes=30)
        await self.monitor.check_once()
        self.assertIn('0 failed sources', self.monitor.state.last_result)

    async def test_sleep_start_wake_and_stop(self):
        await self.monitor.set_sleep(SleepPeriod.parse('23:00-08:00'))
        self.now = local('2026-10-22T23:00')
        await self.monitor.start(12345)
        await self.monitor.check_once()
        self.search.assert_not_awaited()
        self.assertEqual(self.monitor.next_check(), local('2026-10-23T08:00'))
        self.assertIn('Sleeping until 2026-10-23 08:00', self.monitor.status())
        self.now = local('2026-10-23T08:00')
        await self.monitor.check_once()
        self.search.assert_awaited_once()
        await self.monitor.stop()
        self.now += timedelta(days=1)
        self.assertFalse(self.monitor.can_search())

    async def test_sleep_at_end_of_check_saves_wake_schedule_for_restart(self):
        self.now = local('2026-10-22T22:50')
        self.monitor.interval = timedelta(hours=12)
        await self.monitor.set_sleep(SleepPeriod.parse('23:00-08:00'))
        await self.monitor.start(12345)
        await self.monitor.check_once()
        self.assertEqual(self.store.state.next_check_at, local('2026-10-23T08:00'))
        self.now = local('2026-10-23T08:00')
        restarted = VacancyMonitor(self.store, await self.store.load(), 720, self.search,
                                   self.reporter, now=lambda: self.now)
        self.assertEqual(restarted.next_check(), self.now)

    async def test_stop_and_sleep_changes_during_search_are_preserved(self):
        async def search(can_search, on_source):
            on_source('Example')
            self.assertIn('Current source: Example', self.monitor.status())
            await self.monitor.stop()
            self.assertFalse(can_search())
            self.assertIn('Finishing the current source', self.monitor.status())
            await self.monitor.set_sleep(SleepPeriod.parse('23:00-08:00'))
            return SearchResult(delivered=1, interrupted=True)
        self.search.side_effect = search
        await self.monitor.start(12345)
        await self.monitor.check_once()
        self.assertFalse(self.store.state.enabled)
        self.assertEqual(str(self.store.state.sleep), '23:00-08:00')
        self.assertIn('paused before all sources completed', self.monitor.status())

    async def test_failed_settings_save_leaves_live_state_unchanged(self):
        with patch.object(self.store, 'save', new=AsyncMock(side_effect=RuntimeError('offline'))):
            with self.assertRaises(RuntimeError):
                await self.monitor.start(12345)
            self.assertFalse(self.monitor.state.enabled)
            with self.assertRaises(RuntimeError):
                await self.monitor.set_sleep(SleepPeriod.parse('23:00-08:00'))
            self.assertIsNone(self.monitor.state.sleep)

    async def test_worker_wakes_on_start_and_sleep_end_without_overlap(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def search(can_search, on_source):
            started.set()
            await release.wait()
            return SearchResult()
        self.search.side_effect = search
        await self.monitor.set_sleep(SleepPeriod.parse('23:00-08:00'))
        self.now = local('2026-10-23T07:59')
        worker = asyncio.create_task(self.monitor.run())
        try:
            await self.monitor.start(12345)
            await asyncio.sleep(0)
            self.search.assert_not_awaited()
            self.now = local('2026-10-23T08:00')
            self.monitor._wake.set()
            await asyncio.wait_for(started.wait(), 1)
            self.assertFalse(await self.monitor.start(12345))
            self.search.assert_awaited_once()
            self.monitor.close()
            self.assertFalse(worker.done())
            release.set()
            await asyncio.wait_for(worker, 1)
            self.assertTrue(self.store.state.enabled)  # Shutdown is not /stop.
        finally:
            release.set()
            self.monitor.close()
            await worker

    async def test_real_worker_repeats_after_interval(self):
        repeated = asyncio.Event()
        calls = 0
        async def search(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.monitor.close()
                repeated.set()
            return SearchResult()
        self.monitor.now = lambda: datetime.now(timezone.utc)
        self.monitor.interval = timedelta(milliseconds=5)
        self.monitor.search = search
        await self.monitor.start(12345)
        worker = asyncio.create_task(self.monitor.run())
        try:
            await asyncio.wait_for(repeated.wait(), 1)
            await asyncio.wait_for(worker, 1)
            self.assertEqual(calls, 2)
        finally:
            self.monitor.close()
            await worker

    async def test_worker_recovers_from_database_failure(self):
        self.monitor.interval = timedelta(milliseconds=5)
        completed = asyncio.Event()
        async def search(*args):
            self.monitor.close()
            completed.set()
            return SearchResult()
        self.monitor.search = search
        await self.monitor.start(12345)
        save = self.store.save
        calls = 0
        async def intermittent_save(state):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError('database unavailable')
            await save(state)
        self.store.save = intermittent_save
        worker = asyncio.create_task(self.monitor.run())
        try:
            await asyncio.wait_for(completed.wait(), 1)
            await asyncio.wait_for(worker, 1)
            self.reporter.report.assert_called_once()
            self.assertFalse(self.monitor.running)
        finally:
            self.monitor.close()
            await worker

    def test_invalid_intervals(self):
        for interval in (0, -1, float('nan'), float('inf')):
            with self.subTest(interval=interval), self.assertRaises(ValueError):
                VacancyMonitor(self.store, self.store.state, interval, self.search, self.reporter)


class DatabaseMonitoringTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.engine = create_engine(f'sqlite:///{Path(temporary.name) / "state.sqlite3"}')
        Base.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)

    async def test_settings_and_results_survive_new_store_and_can_be_cleared(self):
        store = MysqlMonitoringStore(self.engine)
        self.assertEqual(await store.load(), MonitoringState())
        state = MonitoringState(enabled=True, chat_id=12345, sleep=SleepPeriod.parse('23:00-08:00'),
                                next_check_at=local('2026-10-23T08:00'),
                                last_started_at=local('2026-10-22T22:00'),
                                last_completed_at=local('2026-10-22T22:05'),
                                last_result='2 new vacancies; 1 failed sources')
        await store.save(state)
        other = MysqlMonitoringStore(self.engine)
        self.assertEqual(await other.load(), state)
        cleared = replace(state, enabled=False, sleep=None, next_check_at=None)
        await other.save(cleared)
        self.assertEqual(await store.load(), cleared)

    async def test_threaded_history_keeps_global_delivery_and_utc_checkpoints(self):
        first = ThreadedHistoryRepository(self.engine)
        second = ThreadedHistoryRepository(self.engine)
        job = Job('Title', 'Description', '-', None, 'https://example.com/job', 'fingerprint')
        self.assertFalse(await first.seen(job))
        await first.add_seen_job(job)
        self.assertTrue(await second.seen(job))
        started = local('2026-10-22T22:00')
        await first.save_last_search_time('source', started)
        self.assertEqual(await second.get_last_search_time('source'), started)
        self.assertIsNone(await first.get_last_search_time('other'))

    async def test_earlier_settings_schema_is_upgraded_without_losing_history(self):
        MonitoringModel.__table__.drop(self.engine)
        with self.engine.begin() as connection:
            connection.execute(text('''CREATE TABLE monitoring_settings (
                id INTEGER PRIMARY KEY, enabled BOOLEAN NOT NULL,
                sleep_start INTEGER, sleep_end INTEGER, next_check_at DATETIME,
                last_started_at DATETIME, last_completed_at DATETIME,
                last_result VARCHAR(500) NOT NULL
            )'''))
            connection.execute(text('''INSERT INTO monitoring_settings
                (id, enabled, sleep_start, sleep_end, last_result)
                VALUES (1, 1, 1380, 480, 'Previous check')'''))
        history = ThreadedHistoryRepository(self.engine)
        started = local('2026-10-22T22:00')
        await history.save_last_search_time('source', started)
        store = MysqlMonitoringStore(self.engine)
        state = await store.load()
        self.assertTrue(state.enabled)
        self.assertIsNone(state.chat_id)
        self.assertEqual(str(state.sleep), '23:00-08:00')
        self.assertEqual(state.last_result, 'Previous check')
        self.assertEqual(await history.get_last_search_time('source'), started)
        updated = replace(state, chat_id=2**40)
        await store.save(updated)
        self.assertEqual(await MysqlMonitoringStore(self.engine).load(), updated)

    async def test_slow_database_does_not_block_event_loop(self):
        entered, release = threading.Event(), threading.Event()
        store = MysqlMonitoringStore(self.engine)
        def slow_load():
            entered.set()
            release.wait(timeout=2)
            return MonitoringState()
        with patch.object(store, '_load', side_effect=slow_load):
            task = asyncio.create_task(store.load())
            try:
                await asyncio.wait_for(asyncio.to_thread(entered.wait, 1), 1.5)
                self.assertTrue(entered.is_set())
                self.assertFalse(task.done())
            finally:
                release.set()
                await task
