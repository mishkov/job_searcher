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

from src.error_reporting import ConsoleErrorReporter, posting_context, report_async_errors, update_error_context
from src.job_sources.error_reporting_source import ErrorReportingJobsSource, source_error_context
from src.job_sources.job_source import JobsSource
from src.job_sources.andersen.andersen import AndersenFetchError, AndersenJobsSource, AndersenJobsSourceConfig
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

error_reporter = ConsoleErrorReporter()

nodesk_api: NoDeskApi = PythonRequestsModuleNoDeskApi()
relocate_me_api: RelocateMeApi = PythonRequestsModuleRelocateMeApi()
wwr_api: WeWorkRemotelyApi = PythonRequestsModuleWeWorkRemotelyApi()
cache_path = os.environ.get('CACHE_PATH')
if cache_path and cache_path.strip():
    nodesk_api = SqlCachedNoDeskApi(nodesk_api, cache_path, reporter=error_reporter)
    relocate_me_api = SqlCachedRelocateMeApi(relocate_me_api, cache_path, reporter=error_reporter)
    wwr_api = SqlCachedWeWorkRemotelyApi(wwr_api, cache_path, reporter=error_reporter)

IS_DEBUG_MODE = os.environ["IS_DEBUG_MODE"].strip().lower() in {"1", "true", "yes", "on"}
search_lock = asyncio.Lock()

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
@report_async_errors(error_reporter, operation='start_command')
async def start(message: Message):
    await message.answer("Welcome!", reply_markup=keyboard)

@dp.message(Command("search"))
@report_async_errors(error_reporter, operation='search')
async def jobs(message: Message):
    # A search owns the Telegram client and delivery/history sequence until done.
    async with search_lock:
        await _search_jobs(message)


async def _search_jobs(message: Message):
    status_text ='🔎 Starting jobs search...'
    update_error_context(operation='send_search_status')
    status_message = await message.answer(status_text)

    update_error_context(operation='connect_telegram')
    async with client:
        status_text = '🗃️ Starting database...'
        update_error_context(operation='open_history')
        with Session(engine) as session:
            repository = MysqlJobsSearchHistoryRespository(session)

            foundJobsCount = 0

            status_text = '🔎 Searching in sources...\n'
            for source_config in config.SOURCES:
                source_key = source_config.history_key
                update_error_context(**source_error_context(source_config), since=None,
                                     operation='read_search_history', posting=None)
                since = await repository.get_last_search_time(source_key) if not IS_DEBUG_MODE else None
                source_since = since if since else datetime.now(timezone.utc) - timedelta(days=7)
                update_error_context(since=source_since.isoformat(), operation='source_status')
                status_text_per_source = f'{status_text}\n🔎 {source_config.name}'
                await status_message.edit_text(status_text_per_source)

                update_error_context(operation='create_source')
                source: JobsSource
                if isinstance(source_config, TelegramChannelJobsSourceConfig):
                    source = TelegramChannelJobsSource(client, source_config)
                elif isinstance(source_config, GeekJobRuJobsSourceConfig):
                    source = GeekJobRuJobsSource(source_config)
                elif isinstance(source_config, Web3CareerJobsSourceConfig):
                    source = Web3CareerJobsSource(source_config)
                elif isinstance(source_config, RemoteOkJobsSourceConfig):
                    source = RemoteOkJobsSource(source_config)
                elif isinstance(source_config, AndersenJobsSourceConfig):
                    source = AndersenJobsSource(source_config)
                elif isinstance(source_config, NoDeskJobsSourceConfig):
                    source = NoDeskJobsSource(source_config, nodesk_api)
                elif isinstance(source_config, RelocateMeJobsSourceConfig):
                    source = RelocateMeJobsSource(source_config, relocate_me_api)
                elif isinstance(source_config, WelcomeToTheJungleJobsSourceConfig):
                    source = WelcomeToTheJungleJobsSource(source_config)
                elif isinstance(source_config, WeWorkRemotelyJobsSourceConfig):
                    source = WeWorkRemotelyJobsSource(source_config, wwr_api)
                else:
                    raise ValueError(f"Unknown source: {type(source_config)}")


                source = ErrorReportingJobsSource(source, error_reporter)
                started_at = datetime.now(timezone.utc)
                try:
                    jobs = await source.fetch(since=source_since)
                except (
                    Web3CareerFetchError, WelcomeToTheJungleFetchError, RemoteOkFetchError,
                    NoDeskFetchError, WeWorkRemotelyFetchError, RelocateMeFetchError,
                    AndersenFetchError,
                ) as error:
                    update_error_context(operation='source_status', posting=None)
                    status_text = f"{status_text}\n❌ {source_config.name} [Failed: {str(error)[:300]}]"
                    await status_message.edit_text(status_text)
                    continue

                sourceFoundJobs = 0
                for job in jobs:
                    update_error_context(operation='check_seen_job', posting=posting_context(vars(job)))

                    if not IS_DEBUG_MODE and await repository.seen(job):
                        continue

                    update_error_context(operation='send_job')
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
                        update_error_context(operation='save_seen_job')
                        await repository.add_seen_job(job)
                    sourceFoundJobs += 1

                    update_error_context(operation='source_status')
                    status_text_per_source = f"{status_text}\n⏳ {source_config.name} [Found: {sourceFoundJobs}]"
                    await status_message.edit_text(status_text_per_source)

                if not IS_DEBUG_MODE:
                    update_error_context(operation='save_search_time', posting=None)
                    await repository.save_last_search_time(source_key, started_at)

                update_error_context(operation='source_status', posting=None)
                foundJobsCount += sourceFoundJobs

                status_text = f"{status_text}\n✅ {source_config.name} [Found: {sourceFoundJobs}]"
                await status_message.edit_text(status_text)


            update_error_context(operation='send_search_summary', source=None, source_type=None,
                                 filters=None, posting=None)
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

@report_async_errors(error_reporter, operation='startup')
async def main():
    print(f'Running in {'DEBUG' if IS_DEBUG_MODE else 'RELEASE'} mode')
    print('Initializign database...')
    update_error_context(operation='initialize_database')
    Base.metadata.create_all(engine)
    print('Database Initialized')

    update_error_context(operation='create_bot')
    bot = Bot(token=os.environ['TELEGRAM_BOT_TOKEN'])
    print('Starting telegram bot...')
    update_error_context(operation='poll_telegram')
    await dp.start_polling(bot)
    print('Telegram bot stopped')

if __name__ == '__main__':
    asyncio.run(main())
