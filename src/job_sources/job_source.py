from abc import abstractmethod, ABC
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json

from src.job import Job
from src.search_filter import SearchFilter

@dataclass(frozen=True)
class JobsSourceConfig(ABC):
    name: str
    filters: SearchFilter

    @property
    def history_identity(self) -> str:
        return type(self).__name__

    @property
    def history_key(self) -> str:
        # Explicit fields keep credentials and display names out of persisted identity.
        identity = {
            'source': self.history_identity,
            'keywords': sorted({word.lower() for word in self.filters.keywords}),
            'exclude_keywords': sorted({word.lower() for word in self.filters.exclude_keywords}),
        }
        payload = json.dumps(identity, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(payload.encode()).hexdigest()

class JobsSource[ConfigT: JobsSourceConfig](ABC):
    def __init__(self, config: ConfigT):
        self.config = config

    @abstractmethod
    async def fetch(self, since: datetime) -> list[Job]:
        pass
