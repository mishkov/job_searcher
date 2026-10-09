import os
import asyncio
from dotenv import load_dotenv
from datetime import datetime, timezone
from telethon import TelegramClient, events

from src.search_filter import SearchFilter

from src.job_sources.geek_job_ru.geek_job_ru import GeekJobRuJobsSource
from src.job_sources.telegram_channel_direct.telegram_channel_direct import TelegramChannelDirectJobsSource

load_dotenv()

client = TelegramClient(
    os.environ["PATH_TO_TELEGRAM_SESSION"],
    int(os.environ["TELEGRAM_APP_API_ID"]),
    os.environ["TELEGRAM_APP_API_HASH"],
)

async def main():
    async with client:
        sources = [
            GeekJobRuJobsSource(),
            TelegramChannelDirectJobsSource(client, "flutter_dart_jobs_vakansii")
        ]

        for source in sources:
            since = datetime(2026, 9, 1,tzinfo=timezone.utc)
            jobs = await source.fetch(since=since, search_filter=SearchFilter(keywords=['flutter']))

            if len(jobs) == 0:
                continue

            for job in jobs:
                print(job)
        

asyncio.run(main())
