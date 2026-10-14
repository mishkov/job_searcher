from abc import ABC, abstractmethod
from datetime import datetime

from src.job import Job


class JobsSearchHistoryRepository(ABC):
    @abstractmethod
    async def save_last_search_time(self, source_key: str, started_at: datetime):
        pass

    @abstractmethod
    async def get_last_search_time(self, source_key: str) -> datetime | None:
        pass

    @abstractmethod
    async def add_seen_job(self, job: Job):
        pass

    @abstractmethod
    async def seen(self, job: Job) -> bool:
        pass