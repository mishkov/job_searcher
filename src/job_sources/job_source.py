from abc import abstractmethod, ABC
from dataclasses import dataclass
from datetime import datetime

from src.job import Job
from src.search_filter import SearchFilter

@dataclass(frozen=True)
class JobsSourceConfig(ABC):
    name: str
    filters: SearchFilter

class JobsSource[ConfigT: JobsSourceConfig](ABC):
    def __init__(self, config: ConfigT):
        self.config = config

    @abstractmethod
    async def fetch(self, since: datetime) -> list[Job]:
        pass
