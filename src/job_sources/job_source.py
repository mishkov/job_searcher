from abc import abstractmethod, ABC
from datetime import datetime

from src.job import Job
from src.search_filter import SearchFilter

class JobsSource(ABC):
    def __init__(self, name: str, filters: SearchFilter):
        self.name = name
        self.filters = filters

    @abstractmethod
    async def fetch(self, since: datetime) -> list[Job]:
        pass
