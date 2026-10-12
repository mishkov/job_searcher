from datetime import datetime
import hashlib

from telethon import TelegramClient
from telethon.hints import EntityLike
from telethon.tl.types import Channel

from src.job import Job
from src.job_sources.job_source import JobsSource, JobsSourceConfig
from src.search_filter import SearchFilter

class TelegramChannelJobsSourceConfig(JobsSourceConfig):
    def __init__(self, entity: EntityLike, filters: SearchFilter, forum_id: int | None = None):
        if isinstance(entity, str) and entity.startswith('@'):
            entity = entity.removeprefix('@')

        super().__init__(
            f'TG Channel: @{entity}{f'[{str(forum_id)}]' if forum_id else ''}',
            filters
        )

        self.entity = entity
        self.forum_id = forum_id

class TelegramChannelJobsSource(JobsSource[TelegramChannelJobsSourceConfig]):
    def __init__(self, client: TelegramClient, config: TelegramChannelJobsSourceConfig):
        super().__init__(config)

        self.client = client

    async def fetch(self, since: datetime) -> list[Job]:
        result: list[Job] = []

        messages_iter = self.client.iter_messages(self.config.entity, reply_to=self.config.forum_id) if self.config.forum_id  else self.client.iter_messages(self.config.entity) 
        async for message in messages_iter:
            if message.date < since:
                break

            if not message.text:
                continue

            has_exclude_keyword = False
            for keyword in self.config.filters.exclude_keywords:
                if keyword in message.text:
                    has_exclude_keyword = True
                    break
            if has_exclude_keyword:
                continue

            if not '*' in self.config.filters.keywords:
                has_keyword = False
                for keyword in self.config.filters.keywords:
                    if keyword in message.text:
                        has_keyword = True

                        break

                if not has_keyword:
                    continue

            url = await self._get_message_url(message)
            result.append(Job(
                '-',
                message.text,
                '-',
                None,
                url,
                fingerprint=hashlib.sha256(url.encode()).hexdigest()
            ))

        return result

    async def _get_message_url(self, message) -> str:
        chat = await message.get_chat()

        if not isinstance(chat, Channel):
            return 'https://t.me/{self.channelUsername}'

        if chat.username:
            return f"https://t.me/{chat.username}/{message.id}"

        return f"https://t.me/c/{chat.id}/{message.id}"