from abc import abstractmethod, ABC
from datetime import datetime

from src.job import Job
from src.search_filter import SearchFilter

class JobsSource(ABC):
    @abstractmethod
    async def fetch(self, since: datetime, search_filter: SearchFilter) -> list[Job]:
        pass
