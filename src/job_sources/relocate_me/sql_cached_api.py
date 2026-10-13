import json
import logging
import math
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from src.job_sources.relocate_me.api import RelocateMeApi, validate_posting


logger = logging.getLogger(__name__)


class SqlCachedRelocateMeApi(RelocateMeApi):
    def __init__(self, api: RelocateMeApi, cache_path: str | Path, *, ttl_seconds: float = 24 * 60 * 60):
        if not math.isfinite(ttl_seconds) or ttl_seconds <= 0:
            raise ValueError('Cache TTL must be positive and finite')
        self._api = api
        self._database_path = Path(cache_path).expanduser().absolute() / 'relocate_me' / 'details-v1.sqlite3'
        self._ttl_seconds = ttl_seconds

    def get_jobs_page(self, page: int) -> dict:
        # Discovery must stay fresh so cache hits never hide new vacancies.
        return self._api.get_jobs_page(page)

    def get_job_posting(self, url: str) -> dict | None:
        cached = self._read(url)
        if cached is not None:
            return cached
        # Upstream failures propagate; only cache failures are optional.
        posting = self._api.get_job_posting(url)
        if posting is not None:
            self._write(url, posting)
        return posting

    def _read(self, url: str) -> dict | None:
        try:
            if not self._database_path.is_file():
                return None
            # Read-only mode avoids creating an empty database during a deletion race.
            uri = self._database_path.as_uri() + '?mode=ro'
            with closing(sqlite3.connect(uri, uri=True, timeout=0.1)) as database:
                row = database.execute(
                    'SELECT cached_at, payload FROM job_details WHERE url = ?', (url,),
                ).fetchone()
            if row is None:
                return None
            cached_at, payload = row
            if not isinstance(cached_at, (int, float)) or not math.isfinite(cached_at):
                return None
            if not 0 <= time.time() - cached_at < self._ttl_seconds:
                return None
            posting = json.loads(payload)
            validate_posting(posting)
            return posting
        except (OSError, sqlite3.Error, ValueError, TypeError):
            logger.debug('Relocate.me cache read failed; fetching from API', exc_info=True)
            return None

    def _write(self, url: str, posting: dict):
        try:
            validate_posting(posting)
            payload = json.dumps(posting, allow_nan=False)
            now = time.time()
            cached_at = now
            # Recreate the directory on every write; it may disappear at any time.
            self._database_path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self._database_path, timeout=0.1)) as database:
                database.execute('''
                    CREATE TABLE IF NOT EXISTS job_details (
                        url TEXT PRIMARY KEY,
                        cached_at REAL NOT NULL,
                        payload TEXT NOT NULL
                    )
                ''')
                database.execute('''
                    INSERT INTO job_details (url, cached_at, payload) VALUES (?, ?, ?)
                    ON CONFLICT(url) DO UPDATE SET
                        cached_at = excluded.cached_at, payload = excluded.payload
                ''', (url, cached_at, payload))
                database.execute('DELETE FROM job_details WHERE cached_at <= ?',
                                 (now - self._ttl_seconds,))
                database.commit()
        except (OSError, sqlite3.Error, ValueError, TypeError):
            logger.debug('Relocate.me cache write failed; using API response', exc_info=True)
