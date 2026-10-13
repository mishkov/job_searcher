import json
import logging
import math
import sqlite3
import time
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

from src.job_sources.we_work_remotely.api import (
    FeedResponse, WeWorkRemotelyApi, WeWorkRemotelyFetchError, validate_jobs,
)


logger = logging.getLogger(__name__)


class SqlCachedWeWorkRemotelyApi(WeWorkRemotelyApi):
    def __init__(self, api: WeWorkRemotelyApi, cache_path: str | Path,
                 *, ttl_seconds: float = 24 * 60 * 60):
        if not math.isfinite(ttl_seconds) or ttl_seconds <= 0:
            raise ValueError('Cache TTL must be positive and finite')
        self._api = api
        self._database_path = Path(cache_path).expanduser().absolute() / 'we_work_remotely' / 'feeds-v1.sqlite3'
        self._ttl_seconds = ttl_seconds

    def get_feed(self, category: str, *, etag: str | None = None,
                 last_modified: str | None = None) -> FeedResponse:
        if etag or last_modified:
            return self._api.get_feed(category, etag=etag, last_modified=last_modified)
        cached = self._read(category)
        # Always revalidate with WWR so caching cannot conceal newly posted or removed jobs.
        response = self._api.get_feed(
            category, etag=cached.etag if cached else None,
            last_modified=cached.last_modified if cached else None,
        )
        if response.jobs is None:
            if cached is None:
                raise WeWorkRemotelyFetchError('We Work Remotely returned an unexpected HTTP 304')
            response = FeedResponse(
                cached.jobs, response.etag or cached.etag,
                response.last_modified or cached.last_modified,
            )
        if response.etag or response.last_modified:
            self._write(category, response)
        return response

    def _read(self, category: str) -> FeedResponse | None:
        try:
            if not self._database_path.is_file():
                return None
            with closing(sqlite3.connect(self._database_path.as_uri() + '?mode=ro', uri=True,
                                         timeout=0.1)) as database:
                row = database.execute(
                    'SELECT cached_at, payload FROM feeds WHERE category = ?', (category,),
                ).fetchone()
            if row is None:
                return None
            cached_at, payload = row
            if (not isinstance(cached_at, (int, float)) or not math.isfinite(cached_at)
                    or not 0 <= time.time() - cached_at < self._ttl_seconds):
                return None
            data = json.loads(payload)
            if not isinstance(data, dict):
                return None
            jobs = validate_jobs(data['jobs'])
            for field in ('etag', 'last_modified'):
                if data[field] is not None and (not isinstance(data[field], str)
                                               or '\n' in data[field] or '\r' in data[field]):
                    return None
            if not (data['etag'] or data['last_modified']):
                return None
            return FeedResponse(jobs, data['etag'], data['last_modified'])
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, IndexError, OverflowError):
            logger.debug('WWR cache read failed; fetching RSS', exc_info=True)
            return None

    def _write(self, category: str, response: FeedResponse):
        try:
            validate_jobs(response.jobs)
            payload = json.dumps(asdict(response), allow_nan=False)
            now = time.time()
            self._database_path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self._database_path, timeout=0.1)) as database:
                database.execute('''
                    CREATE TABLE IF NOT EXISTS feeds (
                        category TEXT PRIMARY KEY,
                        cached_at REAL NOT NULL,
                        payload TEXT NOT NULL
                    )
                ''')
                database.execute('''
                    INSERT INTO feeds (category, cached_at, payload) VALUES (?, ?, ?)
                    ON CONFLICT(category) DO UPDATE SET
                        cached_at = excluded.cached_at, payload = excluded.payload
                ''', (category, now, payload))
                database.execute('DELETE FROM feeds WHERE cached_at <= ?',
                                 (now - self._ttl_seconds,))
                database.commit()
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, IndexError, OverflowError):
            logger.debug('WWR cache write failed; using RSS response', exc_info=True)
