from datetime import datetime, timezone

from sqlalchemy import select, exists
from sqlalchemy.orm import Session

from src.job import Job
from src.storage.jobs_search_history_repository import JobsSearchHistoryRepository
from src.storage.mysql.seen_job_model import SeenJobModel
from src.storage.mysql.source_search_history_model import SourceSearchHistoryModel

class MysqlJobsSearchHistoryRespository(JobsSearchHistoryRepository):
    def __init__(self, session: Session):
        self.session = session

    async def save_last_search_time(self, source_key: str, started_at: datetime):
        # MySQL DATETIME has no timezone; store UTC and restore it on reads.
        utc_started_at = (started_at.replace(tzinfo=timezone.utc) if started_at.tzinfo is None
                          else started_at.astimezone(timezone.utc))
        stored_time = utc_started_at.replace(tzinfo=None)
        state = self.session.get(SourceSearchHistoryModel, source_key)
        if state is None:
            self.session.add(SourceSearchHistoryModel(
                source_key=source_key, last_successful_started_at=stored_time,
            ))
        else:
            state.last_successful_started_at = stored_time
        self.session.commit()

    async def get_last_search_time(self, source_key: str) -> datetime | None:
        state = self.session.get(SourceSearchHistoryModel, source_key)
        if state is None:
            return None
        return state.last_successful_started_at.replace(tzinfo=timezone.utc)

    async def add_seen_job(self, job: Job):
        self.session.add(SeenJobModel(job_fingerprint=job.fingerprint))
        self.session.commit()

    async def seen(self, job: Job) -> bool:
        result = self.session.scalar(select(exists().where(
            SeenJobModel.job_fingerprint == job.fingerprint
        )))

        return result if result else False

        
