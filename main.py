import os
import asyncio
from dotenv import load_dotenv
from datetime import datetime, timezone
from telethon import TelegramClient, events

import asyncio
from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart, Command
from aiogram.types import Message

from src.job_sources.job_source import JobsSource
from src.search_filter import SearchFilter

from src.job_sources.geek_job_ru.geek_job_ru import GeekJobRuJobsSource
from src.job_sources.telegram_channel_direct.telegram_channel_direct import TelegramChannelDirectJobsSource

load_dotenv()

dp = Dispatcher()

client = TelegramClient(
    os.environ["PATH_TO_TELEGRAM_SESSION"],
    int(os.environ["TELEGRAM_APP_API_ID"]),
    os.environ["TELEGRAM_APP_API_HASH"],
)

@dp.message(CommandStart())
async def start(message: Message):
    await message.answer("Hello! I'm your bot.")

@dp.message(Command("search"))
async def jobs(message: Message):
    async with client:
        sources: list[JobsSource] = [
            GeekJobRuJobsSource(),
            TelegramChannelDirectJobsSource(client, "flutter_dart_jobs_vakansii")
        ]

        for source in sources:
            since = datetime(2026, 9, 1,tzinfo=timezone.utc)
            jobs = await source.fetch(since=since, search_filter=SearchFilter(keywords=['flutter']))

            if len(jobs) == 0:
                continue

            for job in jobs:
                await message.answer(job.description[:300])

async def main():
    bot = Bot(token=os.environ['TELEGRAM_BOT_TOKEN'])
    await dp.start_polling(bot)

asyncio.run(main())
