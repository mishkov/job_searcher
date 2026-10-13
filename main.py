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
from src.job_sources.telegram_channel.telegram_channel import TelegramChannelJobsSource, TelegramChannelJobsSourceConfig
from src.search_filter import SearchFilter

from src.job_sources.geek_job_ru.geek_job_ru import GeekJobRuJobsSource, GeekJobRuJobsSourceConfig
from src.job_sources.web3_career.web3_career import Web3CareerFetchError, Web3CareerJobsSource, Web3CareerJobsSourceConfig
from src.job_sources.remote_ok.remote_ok import RemoteOkFetchError, RemoteOkJobsSource, RemoteOkJobsSourceConfig
from src.job_sources.nodesk.nodesk import NoDeskFetchError, NoDeskJobsSource, NoDeskJobsSourceConfig
from src.job_sources.nodesk.api import NoDeskApi
from src.job_sources.nodesk.python_requests_api import PythonRequestsModuleNoDeskApi
from src.job_sources.nodesk.sql_cached_api import SqlCachedNoDeskApi
from src.job_sources.relocate_me.api import RelocateMeApi, RelocateMeFetchError
from src.job_sources.relocate_me.python_requests_api import PythonRequestsModuleRelocateMeApi
from src.job_sources.relocate_me.relocate_me import RelocateMeJobsSource, RelocateMeJobsSourceConfig
from src.job_sources.relocate_me.sql_cached_api import SqlCachedRelocateMeApi
from src.job_sources.welcome_to_the_jungle.welcome_to_the_jungle import (
    WelcomeToTheJungleFetchError,
    WelcomeToTheJungleJobsSource,
    WelcomeToTheJungleJobsSourceConfig,
)

from src.job_sources.we_work_remotely.api import WeWorkRemotelyApi, WeWorkRemotelyFetchError
from src.job_sources.we_work_remotely.python_requests_api import PythonRequestsModuleWeWorkRemotelyApi
from src.job_sources.we_work_remotely.sql_cached_api import SqlCachedWeWorkRemotelyApi
from src.job_sources.we_work_remotely.we_work_remotely import (
    WeWorkRemotelyJobsSource, WeWorkRemotelyJobsSourceConfig,
)

from src.storage.mysql.base import Base
from src.storage.mysql.mysql_jobs_search_history_repository import MysqlJobsSearchHistoryRespository

import config


load_dotenv()

nodesk_api: NoDeskApi = PythonRequestsModuleNoDeskApi()
relocate_me_api: RelocateMeApi = PythonRequestsModuleRelocateMeApi()
wwr_api: WeWorkRemotelyApi = PythonRequestsModuleWeWorkRemotelyApi()
cache_path = os.environ.get('CACHE_PATH')
if cache_path and cache_path.strip():
    nodesk_api = SqlCachedNoDeskApi(nodesk_api, cache_path)
    relocate_me_api = SqlCachedRelocateMeApi(relocate_me_api, cache_path)
    wwr_api = SqlCachedWeWorkRemotelyApi(wwr_api, cache_path)

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
            if not IS_DEBUG_MODE:
                await repository.save_last_search_time(datetime.now())

            foundJobsCount = 0

            status_text = '🔎 Searching in sources...\n'
            for source_config in config.SOURCES:
                status_text_per_source = f'{status_text}\n🔎 {source_config.name}'
                await status_message.edit_text(status_text_per_source)

                source: JobsSource
                if isinstance(source_config, TelegramChannelJobsSourceConfig):
                    source = TelegramChannelJobsSource(client, source_config)
                elif isinstance(source_config, GeekJobRuJobsSourceConfig):
                    source = GeekJobRuJobsSource(source_config)
                elif isinstance(source_config, Web3CareerJobsSourceConfig):
                    source = Web3CareerJobsSource(source_config)
                elif isinstance(source_config, RemoteOkJobsSourceConfig):
                    source = RemoteOkJobsSource(source_config)
                elif isinstance(source_config, NoDeskJobsSourceConfig):
                    source = NoDeskJobsSource(source_config, nodesk_api)
                elif isinstance(source_config, RelocateMeJobsSourceConfig):
                    source = RelocateMeJobsSource(source_config, relocate_me_api)
                elif isinstance(source_config, WelcomeToTheJungleJobsSourceConfig):
                    source = WelcomeToTheJungleJobsSource(source_config)
                elif isinstance(source_config, WeWorkRemotelyJobsSourceConfig):
                    source = WeWorkRemotelyJobsSource(source_config, wwr_api)
                else:
                    raise ValueError(f"Unknown source: {type(config)}")


                try:
                    jobs = await source.fetch(
                        since=since if since else datetime.now(timezone.utc) - timedelta(days=7)
                    )
                except (
                    Web3CareerFetchError, WelcomeToTheJungleFetchError, RemoteOkFetchError,
                    NoDeskFetchError, WeWorkRemotelyFetchError, RelocateMeFetchError,
                ) as error:
                    status_text = f"{status_text}\n❌ {source_config.name} [Failed: {str(error)[:300]}]"
                    await status_message.edit_text(status_text)
                    continue
                
                if len(jobs) == 0:
                    status_text = f"{status_text}\n✅ {source_config.name} [Found: 0]"
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

                    status_text_per_source = f"{status_text}\n⏳ {source_config.name} [Found: {sourceFoundJobs}]"
                    await status_message.edit_text(status_text_per_source)

                foundJobsCount += sourceFoundJobs
                    
                status_text = f"{status_text}\n✅ {source_config.name} [Found: {sourceFoundJobs}]"
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
