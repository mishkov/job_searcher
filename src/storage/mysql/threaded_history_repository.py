import asyncio
from datetime import datetime

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from src.job import Job
from src.storage.jobs_search_history_repository import JobsSearchHistoryRepository
from src.storage.mysql.mysql_jobs_search_history_repository import MysqlJobsSearchHistoryRespository


class ThreadedHistoryRepository(JobsSearchHistoryRepository):
    """Keep each synchronous SQLAlchemy session entirely within its worker thread."""

    def __init__(self, engine: Engine):
        self.engine = engine

    async def _call(self, operation: str, *args):
        def run():
            with Session(self.engine) as session:
                repository = MysqlJobsSearchHistoryRespository(session)
                return asyncio.run(getattr(repository, operation)(*args))
        return await asyncio.to_thread(run)

    async def get_last_search_time(self, source_key: str) -> datetime | None:
        return await self._call('get_last_search_time', source_key)

    async def save_last_search_time(self, source_key: str, started_at: datetime) -> None:
        await self._call('save_last_search_time', source_key, started_at)

    async def seen(self, job: Job) -> bool:
        return await self._call('seen', job)

    async def add_seen_job(self, job: Job) -> None:
        await self._call('add_seen_job', job)
