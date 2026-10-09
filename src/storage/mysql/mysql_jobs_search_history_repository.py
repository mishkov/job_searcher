from datetime import datetime

from sqlalchemy import select, exists
from sqlalchemy.orm import Session

from src.job import Job
from src.storage.jobs_search_history_repository import JobsSearchHistoryRepository
from src.storage.mysql.seen_job_model import SeenJobModel
from src.storage.mysql.last_search_time_model import LastSearchTimeModel

class MysqlJobsSearchHistoryRespository(JobsSearchHistoryRepository):
    def __init__(self, session: Session):
        self.session = session

    async def save_last_search_time(self, datetime: datetime):
        state = self.session.get(LastSearchTimeModel, 1)

        if state is None:
            state = LastSearchTimeModel(id=1, last_searched_at=datetime)
            self.session.add(state)
        else:
            state.last_searched_at = datetime

        self.session.commit()
    
    async def get_last_search_time(self) -> datetime | None:
        state = self.session.get(LastSearchTimeModel, 1)
        last_search = state.last_searched_at if state else None

    async def add_seen_job(self, job: Job):
        self.session.add(SeenJobModel(job_fingerprint=job.fingerprint))
        self.session.commit()

    async def seen(self, job: Job) -> bool:
        result = self.session.scalar(select(exists().where(
            SeenJobModel.job_fingerprint == job.fingerprint
        )))

        return result if result else False

        
