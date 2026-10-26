import os
import asyncio
import re
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone
from sqlalchemy import create_engine
from telethon import TelegramClient

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand, CallbackQuery, KeyboardButton, Message, ReplyKeyboardMarkup,
)
from aiogram.enums import ParseMode

from src.error_reporting import ConsoleErrorReporter, posting_context, report_async_errors, update_error_context
from src.job_sources.error_reporting_source import ErrorReportingJobsSource, source_error_context
from src.job_sources.job_source import JobsSource
from src.job_sources.comeet.comeet import ComeetFetchError, ComeetJobsSource, ComeetJobsSourceConfig
from src.job_sources.aj_bell.aj_bell import AjBellFetchError, AjBellJobsSource, AjBellJobsSourceConfig
from src.job_sources.andersen.andersen import AndersenFetchError, AndersenJobsSource, AndersenJobsSourceConfig
from src.job_sources.hackmotion.hackmotion import HackMotionFetchError, HackMotionJobsSource, HackMotionJobsSourceConfig
from src.job_sources.greenhouse.greenhouse import GreenhouseFetchError, GreenhouseJobsSource, GreenhouseJobsSourceConfig
from src.job_sources.brightstar_lottery.brightstar_lottery import (
    BrightstarLotteryFetchError, BrightstarLotteryJobsSource, BrightstarLotteryJobsSourceConfig,
)
from src.job_sources.smartrecruiters.smartrecruiters import (
    SmartRecruitersFetchError, SmartRecruitersJobsSource, SmartRecruitersJobsSourceConfig,
)
from src.job_sources.telegram_channel.telegram_channel import TelegramChannelJobsSource, TelegramChannelJobsSourceConfig

from src.job_sources.geek_job_ru.geek_job_ru import GeekJobRuJobsSource, GeekJobRuJobsSourceConfig
from src.job_sources.web3_career.web3_career import Web3CareerFetchError, Web3CareerJobsSource, Web3CareerJobsSourceConfig
from src.job_sources.remote_ok.remote_ok import RemoteOkFetchError, RemoteOkJobsSource, RemoteOkJobsSourceConfig
from src.job_sources.rippling.rippling import RipplingFetchError, RipplingJobsSource, RipplingJobsSourceConfig
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
from src.storage.mysql.threaded_history_repository import ThreadedHistoryRepository
from src.storage.mysql.monitoring_store import MysqlMonitoringStore
from src.storage.mysql.job_post_repository import MysqlJobPostRepository
from src.job_post_actions import ARCHIVE_POST, RESTORE_POST, JobPostActions, job_post_keyboard
from src.monitoring import SearchResult, SleepPeriod, VacancyMonitor

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
            KeyboardButton(text="/start"),
            KeyboardButton(text="/stop"),
            KeyboardButton(text="/status"),
        ],
        [KeyboardButton(text="/setsleeptime")],
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

job_post_actions = JobPostActions(MysqlJobPostRepository(engine), error_reporter)

monitor: VacancyMonitor | None = None


class SleepSettings(StatesGroup):
    waiting_for_period = State()


def is_private_chat(message: Message) -> bool:
    return message.chat.type == 'private'


@dp.callback_query(F.data.in_({ARCHIVE_POST, RESTORE_POST}))
@report_async_errors(error_reporter, operation='job_post_callback')
async def job_post_callback(query: CallbackQuery):
    await job_post_actions.handle(query)


@dp.message(CommandStart())
@report_async_errors(error_reporter, operation='start_command')
async def start(message: Message, state: FSMContext):
    if not is_private_chat(message):
        return
    await state.clear()
    enabled = await monitor.start(message.chat.id)
    text = 'Automatic checks enabled.' if enabled else 'Automatic checks are already enabled.'
    await message.answer(f'{text}\n{monitor.status()}', reply_markup=keyboard)


@dp.message(Command('stop'))
@report_async_errors(error_reporter, operation='stop_command')
async def stop(message: Message, state: FSMContext):
    if not is_private_chat(message):
        return
    await state.clear()
    await monitor.stop()
    text = 'Automatic checks paused.'
    if monitor.running:
        text += ' Finishing the current source and its deliveries first.'
    await message.answer(text, reply_markup=keyboard)


@dp.message(Command('status'))
@report_async_errors(error_reporter, operation='status_command')
async def status(message: Message, state: FSMContext):
    if not is_private_chat(message):
        return
    await state.clear()
    await message.answer(monitor.status(), reply_markup=keyboard)


@dp.message(Command('setsleeptime'))
@report_async_errors(error_reporter, operation='set_sleep_command')
async def set_sleep_time(message: Message, state: FSMContext):
    if not is_private_chat(message):
        return
    await message.answer(
        'Send your daily sleep period in 24-hour format: 23:00-08:00.\n'
        'The period can cross midnight. Automatic checks pause during this time.\n'
        'A source already running finishes with its deliveries before pausing.\n'
        f'Timezone: Europe/Minsk\nCurrent sleep period: {monitor.state.sleep or "Off"}\n'
        'Send off to disable sleeping time, or /cancel to keep the current setting.'
    )
    await state.set_state(SleepSettings.waiting_for_period)


@dp.message(Command('cancel'))
@report_async_errors(error_reporter, operation='cancel_command')
async def cancel(message: Message, state: FSMContext):
    if not is_private_chat(message):
        return
    await state.clear()
    await message.answer('Sleeping time unchanged.', reply_markup=keyboard)


@dp.message(SleepSettings.waiting_for_period, F.text, ~F.text.startswith('/'))
@report_async_errors(error_reporter, operation='save_sleep_time')
async def save_sleep_time(message: Message, state: FSMContext):
    if not is_private_chat(message):
        return
    value = message.text.strip()
    try:
        period = None if value.lower() == 'off' else SleepPeriod.parse(value)
    except ValueError as error:
        await message.answer(f'{error} Send /cancel to keep the current setting.')
        return
    await monitor.set_sleep(period)
    await state.clear()
    await message.answer(
        f'Sleeping time saved: {period or "Off"} (Europe/Minsk).\n{monitor.status()}',
        reply_markup=keyboard,
    )


@report_async_errors(error_reporter, operation='search')
async def search_jobs(send_message, should_continue=lambda: True, on_source=lambda name: None) -> SearchResult:
    # A search owns the Telegram client and delivery/history sequence until done.
    async with search_lock:
        return await _search_jobs(send_message, should_continue, on_source)


async def _search_jobs(send_message, should_continue, on_source) -> SearchResult:
    result = SearchResult()
    update_error_context(operation='connect_telegram')
    async with client:
        update_error_context(operation='open_history')
        repository = ThreadedHistoryRepository(engine)
        for source_config in config.SOURCES:
            if not should_continue():
                result.interrupted = True
                break
            on_source(source_config.name)
            source_key = source_config.history_key
            update_error_context(**source_error_context(source_config), since=None,
                                 operation='read_search_history', posting=None)
            since = await repository.get_last_search_time(source_key) if not IS_DEBUG_MODE else None
            source_since = since if since else datetime.now(timezone.utc) - timedelta(days=7)
            update_error_context(since=source_since.isoformat(), operation='create_source')
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
            elif isinstance(source_config, AjBellJobsSourceConfig):
                source = AjBellJobsSource(source_config)
            elif isinstance(source_config, HackMotionJobsSourceConfig):
                source = HackMotionJobsSource(source_config)
            elif isinstance(source_config, GreenhouseJobsSourceConfig):
                source = GreenhouseJobsSource(source_config)
            elif isinstance(source_config, ComeetJobsSourceConfig):
                source = ComeetJobsSource(source_config)
            elif isinstance(source_config, BrightstarLotteryJobsSourceConfig):
                source = BrightstarLotteryJobsSource(source_config)
            elif isinstance(source_config, SmartRecruitersJobsSourceConfig):
                source = SmartRecruitersJobsSource(source_config)
            elif isinstance(source_config, RipplingJobsSourceConfig):
                source = RipplingJobsSource(source_config)
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
                AndersenFetchError, SmartRecruitersFetchError, AjBellFetchError,
                BrightstarLotteryFetchError, RipplingFetchError,
                HackMotionFetchError, GreenhouseFetchError, ComeetFetchError,
            ):
                result.failed_sources += 1
                continue

            for job in jobs:
                update_error_context(operation='check_seen_job', posting=posting_context(vars(job)))
                if not IS_DEBUG_MODE and await repository.seen(job):
                    continue
                update_error_context(operation='send_job')
                keyboard = job_post_keyboard(job.url)
                await send_message(
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
                result.delivered += 1

            if not IS_DEBUG_MODE:
                update_error_context(operation='save_search_time', posting=None)
                await repository.save_last_search_time(source_key, started_at)
    return result


def escape_markdown(text: str) -> str:
    return re.sub(
        r'([_*\[\]()~`>#+\-=|{}.!\\])',
        r'\\\1',
        text
    )

@report_async_errors(error_reporter, operation='startup')
async def main():
    global monitor
    print(f'Running in {"DEBUG" if IS_DEBUG_MODE else "RELEASE"} mode')
    update_error_context(operation='initialize_database')
    await asyncio.to_thread(Base.metadata.create_all, engine)

    update_error_context(operation='configure_monitoring')
    store = MysqlMonitoringStore(engine)
    saved_state = await store.load()
    update_error_context(operation='create_bot')
    async with Bot(token=os.environ['TELEGRAM_BOT_TOKEN']) as bot:
        async def search(should_continue, on_source):
            # Keep a running check's destination stable if another chat sends /start.
            chat_id = monitor.state.chat_id
            async def send(text, **kwargs):
                await bot.send_message(chat_id, text, **kwargs)
            return await search_jobs(send, should_continue, on_source)

        monitor = VacancyMonitor(store, saved_state, float(config.FETCH_PERIOD_IN_MINUTES),
                                 search, error_reporter)
        await bot.set_my_commands([
            BotCommand(command='start', description='Enable automatic vacancy checks'),
            BotCommand(command='stop', description='Pause automatic checks'),
            BotCommand(command='status', description='Show monitoring status'),
            BotCommand(command='setsleeptime', description='Set daily sleeping time'),
            BotCommand(command='cancel', description='Cancel sleeping-time setup'),
        ])
        worker = asyncio.create_task(monitor.run())
        try:
            update_error_context(operation='poll_telegram')
            await dp.start_polling(bot, close_bot_session=False)
        finally:
            monitor.close()
            await worker
            await client.disconnect()
            await asyncio.to_thread(engine.dispose)


if __name__ == '__main__':
    asyncio.run(main())
