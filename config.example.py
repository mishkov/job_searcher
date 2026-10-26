from dataclasses import replace

from src.job_sources.aj_bell.aj_bell import AjBellJobsSourceConfig
from src.job_sources.andersen.andersen import AndersenJobsSourceConfig
from src.job_sources.hackmotion.hackmotion import HackMotionJobsSourceConfig
from src.job_sources.greenhouse.greenhouse import GreenhouseJobsSourceConfig
from src.job_sources.brightstar_lottery.brightstar_lottery import BrightstarLotteryJobsSourceConfig
from src.job_sources.smartrecruiters.smartrecruiters import SmartRecruitersJobsSourceConfig
from src.job_sources.geek_job_ru.geek_job_ru import GeekJobRuJobsSourceConfig
from src.job_sources.job_source import JobsSourceConfig
from src.job_sources.remote_ok.remote_ok import RemoteOkJobsSourceConfig
from src.job_sources.rippling.rippling import RipplingJobsSourceConfig
from src.job_sources.nodesk.nodesk import NoDeskJobsSourceConfig
from src.job_sources.relocate_me.relocate_me import RelocateMeJobsSourceConfig
from src.job_sources.telegram_channel.telegram_channel import TelegramChannelJobsSourceConfig
from src.job_sources.welcome_to_the_jungle.welcome_to_the_jungle import WelcomeToTheJungleJobsSourceConfig
from src.job_sources.we_work_remotely.we_work_remotely import WeWorkRemotelyJobsSourceConfig
from src.search_filter import SearchFilter


FETCH_PERIOD_IN_MINUTES=5

_no_filters = SearchFilter()
_common_filters = SearchFilter(keywords=['flutter'])
_filters_for_groups_with_cv = replace(
    _common_filters,
    exclude_keywords=[
        *_common_filters.exclude_keywords,
        '#резюме ',
        '#resume ',
        '#cv ',
        ]
    )

SOURCES: list[JobsSourceConfig] = [
    AjBellJobsSourceConfig(_common_filters),
    AndersenJobsSourceConfig(_common_filters),
    HackMotionJobsSourceConfig(_common_filters),
    GreenhouseJobsSourceConfig('capitalontap', _common_filters),
    BrightstarLotteryJobsSourceConfig(_common_filters),
    SmartRecruitersJobsSourceConfig(_common_filters),
    RipplingJobsSourceConfig('sparq', _common_filters),
    GeekJobRuJobsSourceConfig(_common_filters),
    RemoteOkJobsSourceConfig(_common_filters),
    NoDeskJobsSourceConfig(_common_filters),
    RelocateMeJobsSourceConfig(_common_filters),
    WeWorkRemotelyJobsSourceConfig(_common_filters),
    WelcomeToTheJungleJobsSourceConfig(_common_filters),

    TelegramChannelJobsSourceConfig("flutter_dart_jobs_vakansii", _no_filters),

    TelegramChannelJobsSourceConfig("evacuatejobs", _common_filters),
    TelegramChannelJobsSourceConfig(entity=-1002495603766, filters=_common_filters, forum_id=5),

    TelegramChannelJobsSourceConfig("mobile_jobs", _filters_for_groups_with_cv),
]
