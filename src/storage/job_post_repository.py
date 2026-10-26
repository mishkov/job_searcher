from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class SavedJobPost:
    chat_id: int
    message_id: int
    title: str
    text: str
    entities: list[dict]
    reply_markup: dict


class JobPostRepository(ABC):
    @abstractmethod
    async def get(self, chat_id: int, message_id: int) -> SavedJobPost | None:
        pass

    @abstractmethod
    async def save(self, post: SavedJobPost) -> SavedJobPost:
        """Save the first snapshot, returning the existing snapshot on retries."""
        pass
