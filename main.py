import os
import asyncio
import re
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from telethon import TelegramClient
from dataclasses import replace

import asyncio
from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiogram.enums import ParseMode

from src.job_sources.job_source import JobsSource
from src.job_sources.telegram_channel.telegram_channel import TelegramChannelJobsSource
from src.search_filter import SearchFilter

from src.job_sources.geek_job_ru.geek_job_ru import GeekJobRuJobsSource

from src.storage.mysql.base import Base
from src.storage.mysql.mysql_jobs_search_history_repository import MysqlJobsSearchHistoryRespository


load_dotenv()

IS_DEBUG_MODE=os.environ["IS_DEBUG_MODE"]

keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text="/search"),
        ]
    ],
    resize_keyboard=True,
    is_persistent=True,
)

dp = Dispatcher()

client = TelegramClient(
    os.environ["PATH_TO_TELEGRAM_SESSION"],
    int(os.environ["TELEGRAM_APP_API_ID"]),
    os.environ["TELEGRAM_APP_API_HASH"],
)

engine = create_engine(
    os.environ['DATABASE_URL']
)

@dp.message(CommandStart())
async def start(message: Message):
    await message.answer("Welcome!", reply_markup=keyboard)

@dp.message(Command("search"))
async def jobs(message: Message):
    status_text ='🔎 Starting jobs search...'
    status_message = await message.answer(status_text)

    async with client:
        status_text = '🗃️ Starting database...'
        with Session(engine) as session:
            status_text = '⏰ Updating last search time...'
            await status_message.edit_text(status_text)
            repository = MysqlJobsSearchHistoryRespository(session)
            since = await repository.get_last_search_time() if not IS_DEBUG_MODE else datetime.now(timezone.utc) - timedelta(days=7)
            await repository.save_last_search_time(datetime.now())

            no_filters = SearchFilter()
            common_filters = SearchFilter(keywords=['flutter'])
            filters_for_groups_with_cv = replace(
                common_filters,
                exclude_keywords=[
                    *common_filters.exclude_keywords,
                    '#резюме ',
                    '#resume ',
                    '#cv ',
                    ]
                )

            sources: list[JobsSource] = [
                GeekJobRuJobsSource('geekjob.ru', common_filters),
                TelegramChannelJobsSource(client, "flutter_dart_jobs_vakansii", no_filters),
                TelegramChannelJobsSource(client, "dartlang_jobs", no_filters),
                TelegramChannelJobsSource(client, "dartuz_jobs", no_filters),

                TelegramChannelJobsSource(client, "evacuatejobs", common_filters),
                TelegramChannelJobsSource(client, "remote_jobs_relocate", common_filters),
                TelegramChannelJobsSource(client, "Remoteit", common_filters),
                TelegramChannelJobsSource(client, "refer_me_it", common_filters),
                TelegramChannelJobsSource(client, "outstaff_requests_phpdev", common_filters),
                TelegramChannelJobsSource(client, "gdjobs", common_filters),
                TelegramChannelJobsSource(client, entity=-1002495603766, filters=common_filters, forum_id=5),

                TelegramChannelJobsSource(client, "mobile_jobs", filters_for_groups_with_cv),
                TelegramChannelJobsSource(client, "hot_itjobs", filters_for_groups_with_cv),
                TelegramChannelJobsSource(client, "web30job", filters_for_groups_with_cv),
            ]

            foundJobsCount = 0

            status_text = '🔎 Searching in sources...\n'
            for source in sources:
                status_text_per_source = f'{status_text}\n🔎 {source.name}'
                await status_message.edit_text(status_text_per_source)
                jobs = await source.fetch(
                    since=since if since else datetime.now(timezone.utc) - timedelta(days=7)
                )
                
                if len(jobs) == 0:
                    status_text = f"{status_text}\n✅ {source.name} [Found: 0]"
                    await status_message.edit_text(status_text)
                    
                    continue

                sourceFoundJobs = 0
                for job in jobs:

                    if await repository.seen(job) and not IS_DEBUG_MODE:
                        continue

                    keyboard = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(text="Open job 🔗", url=job.url)]
                        ]
                    )
                    await message.answer(
                        f'🧑‍💻 *Title:* {escape_markdown(job.title)}\n\n'
                        f'🏝️ _Remote allowed:_ {escape_markdown(str(job.is_remote_allowed))}\n\n'
                        f'💸 _Salary:_ {escape_markdown(job.salary)}\n\n'
                        f'📜 {escape_markdown(job.description[:500])}\\.\\.\\.',
                        parse_mode=ParseMode.MARKDOWN_V2,
                        reply_markup=keyboard
                    )

                    if not IS_DEBUG_MODE:
                        await repository.add_seen_job(job)
                    sourceFoundJobs += 1

                    status_text_per_source = f"{status_text}\n⏳ {source.name} [Found: {sourceFoundJobs}]"
                    await status_message.edit_text(status_text_per_source)
                
                foundJobsCount += sourceFoundJobs
                    
                status_text = f"{status_text}\n✅ {source.name} [Found: {sourceFoundJobs}]"
                await status_message.edit_text(status_text)
                

            if foundJobsCount == 0:
                await message.answer('No new jobs found')
            else:
                await message.answer(f'Found {foundJobsCount} new jobs')

def escape_markdown(text: str) -> str:
    return re.sub(
        r'([_*\[\]()~`>#+\-=|{}.!\\])',
        r'\\\1',
        text
    )

async def main():
    print(f'Running in {'DEBUG' if IS_DEBUG_MODE else 'RELEASE'} mode')
    print('Initializign database...')
    Base.metadata.create_all(engine)
    print('Database Initialized')

    bot = Bot(token=os.environ['TELEGRAM_BOT_TOKEN'])
    print('Starting telegram bot...')
    await dp.start_polling(bot)
    print('Telegram bot stopped')

asyncio.run(main())
