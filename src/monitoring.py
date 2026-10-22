import asyncio
import math
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Protocol
from zoneinfo import ZoneInfo

from src.error_reporting import ErrorReporter, report_error, report_errors


LOCAL_TIMEZONE = ZoneInfo('Europe/Minsk')


@dataclass(frozen=True)
class SleepPeriod:
    start: int
    end: int

    @classmethod
    def parse(cls, value: str) -> 'SleepPeriod':
        match = re.fullmatch(r'([01]\d|2[0-3]):([0-5]\d)\s*-\s*([01]\d|2[0-3]):([0-5]\d)', value.strip())
        if not match:
            raise ValueError('Use HH:MM-HH:MM, for example 23:00-08:00.')
        sh, sm, eh, em = map(int, match.groups())
        period = cls(sh * 60 + sm, eh * 60 + em)
        if period.start == period.end:
            raise ValueError('Start and end must differ. Send off to disable sleeping time.')
        return period

    def __str__(self) -> str:
        return f'{self.start // 60:02}:{self.start % 60:02}-{self.end // 60:02}:{self.end % 60:02}'

    def sleeping_until(self, now: datetime) -> datetime | None:
        local = now.astimezone(LOCAL_TIMEZONE)
        minute = local.hour * 60 + local.minute
        sleeping = (self.start <= minute < self.end if self.start < self.end
                    else minute >= self.start or minute < self.end)
        if not sleeping:
            return None
        end = local.replace(hour=self.end // 60, minute=self.end % 60, second=0, microsecond=0)
        if end <= local:
            end += timedelta(days=1)
        return end.astimezone(timezone.utc)

    def next_check(self, now: datetime, due: datetime) -> datetime:
        if end := self.sleeping_until(now):
            return end
        local = now.astimezone(LOCAL_TIMEZONE)
        start = local.replace(hour=self.start // 60, minute=self.start % 60, second=0, microsecond=0)
        if start <= local:
            start += timedelta(days=1)
        # Wake-up always triggers a catch-up check, even with a long interval.
        if due >= start:
            return self.sleeping_until(start)
        return due


@dataclass(frozen=True)
class MonitoringState:
    enabled: bool = False
    sleep: SleepPeriod | None = None
    next_check_at: datetime | None = None
    last_started_at: datetime | None = None
    last_completed_at: datetime | None = None
    last_result: str = 'No checks yet'


class MonitoringStore(Protocol):
    async def load(self) -> MonitoringState: ...
    async def save(self, state: MonitoringState) -> None: ...


@dataclass
class SearchResult:
    delivered: int = 0
    failed_sources: int = 0
    interrupted: bool = False

    def __str__(self) -> str:
        result = f'{self.delivered} new vacancies; {self.failed_sources} failed sources'
        return f'{result}; paused before all sources completed' if self.interrupted else result


class VacancyMonitor:
    def __init__(self, store: MonitoringStore, state: MonitoringState, interval_minutes: float,
                 search: Callable[[Callable[[], bool], Callable[[str], None]], Awaitable[SearchResult]],
                 reporter: ErrorReporter, *, now: Callable[[], datetime] | None = None):
        if not math.isfinite(interval_minutes) or interval_minutes <= 0:
            raise ValueError('FETCH_PERIOD_IN_MINUTES must be a finite positive number')
        self.store = store
        self.state = state
        self.interval = timedelta(minutes=interval_minutes)
        self.search = search
        self.reporter = reporter
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.current_source: str | None = None
        self.running = False
        self.worker_error: str | None = None
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()
        self._closing = False

    async def _update(self, **changes) -> None:
        # Save before acknowledging commands; failed writes never change live settings.
        async with self._lock:
            state = replace(self.state, **changes)
            await self.store.save(state)
            self.state = state

    async def start(self) -> bool:
        async with self._lock:
            if self.state.enabled:
                return False
            state = replace(self.state, enabled=True, next_check_at=self.now())
            await self.store.save(state)
            self.state = state
        self._wake.set()
        return True

    async def stop(self) -> None:
        await self._update(enabled=False, next_check_at=None)
        self._wake.set()

    async def set_sleep(self, period: SleepPeriod | None) -> None:
        await self._update(sleep=period, next_check_at=self.now())
        self._wake.set()

    def can_search(self) -> bool:
        return (not self._closing and self.state.enabled
                and not (self.state.sleep and self.state.sleep.sleeping_until(self.now())))

    def next_check(self) -> datetime | None:
        if not self.state.enabled:
            return None
        now = self.now()
        due = max(now, self.state.next_check_at or now)
        return self.state.sleep.next_check(now, due) if self.state.sleep else due

    def status(self) -> str:
        if not self.state.enabled:
            mode = 'Paused by you'
        elif self.state.sleep and (end := self.state.sleep.sleeping_until(self.now())):
            mode = f'Sleeping until {self._time(end)}'
        elif self.running:
            mode = 'Searching'
        else:
            mode = 'Waiting for next check'
        if self.running:
            mode += f'\nCurrent source: {self.current_source or "Starting"}'
            if not self.can_search():
                mode += '\nFinishing the current source before pausing.'
        next_time = ('After the current check' if self.running and self.can_search()
                     else self._time(self.next_check()))
        return (f'{mode}\nTimezone: Europe/Minsk\n'
                f'Check interval: {self.interval.total_seconds() / 60:g} minutes after completion\n'
                f'Sleeping time: {self.state.sleep or "Off"}\n'
                f'Next check: {next_time}\n'
                f'Last check started: {self._time(self.state.last_started_at)}\n'
                f'Last check completed: {self._time(self.state.last_completed_at)}\n'
                f'Last result: {self.state.last_result}'
                + (f'\nWorker error: {self.worker_error}; retrying automatically' if self.worker_error else ''))

    @staticmethod
    def _time(value: datetime | None) -> str:
        return value.astimezone(LOCAL_TIMEZONE).strftime('%Y-%m-%d %H:%M') if value else '—'

    def _source(self, name: str) -> None:
        self.current_source = name

    async def check_once(self) -> None:
        if not self.can_search():
            return
        self.running = True
        try:
            await self._update(last_started_at=self.now(), last_result='Check in progress')
            try:
                with report_errors(self.reporter, operation='scheduled_search'):
                    result = str(await self.search(self.can_search, self._source))
            except Exception as error:
                # Detailed diagnostics stay in the error reporter, not Telegram or the DB.
                result = f'Check failed ({type(error).__name__}); retrying on the next check'
            completed = self.now()
            due = completed + self.interval
            if self.state.sleep:
                due = self.state.sleep.next_check(completed, due)
            await self._update(last_completed_at=completed, last_result=result,
                               next_check_at=due)
        finally:
            self.running = False
            self.current_source = None

    async def run(self) -> None:
        while not self._closing:
            self._wake.clear()
            try:
                due = self.next_check()
                if due is not None and due <= self.now():
                    await self.check_once()
                    self.worker_error = None
                    continue
                delay = (due - self.now()).total_seconds() if due else None
            except Exception as error:
                report_error(self.reporter, error, operation='monitoring_worker')
                self.worker_error = type(error).__name__
                delay = self.interval.total_seconds()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            except TimeoutError:
                pass

    def close(self) -> None:
        # Drain the current source so delivery and its checkpoint stay together.
        self._closing = True
        self._wake.set()
