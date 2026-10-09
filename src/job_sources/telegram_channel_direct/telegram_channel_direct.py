from datetime import datetime
import hashlib

from telethon import TelegramClient
from telethon.tl.types import Channel, Message

from src.job import Job
from src.job_sources.job_source import JobsSource
from src.search_filter import SearchFilter

class TelegramChannelDirectJobsSource(JobsSource):
    def __init__(self, client: TelegramClient, channelUsername: str):
        super().__init__('TG Channel: @channelUsername')
        self.client = client
        self.channelUsername = channelUsername

    async def fetch(self, since: datetime, search_filter: SearchFilter) -> list[Job]:
        result: list[Job] = []

        async for message in self.client.iter_messages(self.channelUsername):
            if message.date < since:
                break

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