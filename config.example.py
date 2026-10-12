from dataclasses import replace

from src.job_sources.geek_job_ru.geek_job_ru import GeekJobRuJobsSourceConfig
from src.job_sources.job_source import JobsSourceConfig
from src.job_sources.remote_ok.remote_ok import RemoteOkJobsSourceConfig
from src.job_sources.telegram_channel.telegram_channel import TelegramChannelJobsSourceConfig
from src.job_sources.welcome_to_the_jungle.welcome_to_the_jungle import WelcomeToTheJungleJobsSourceConfig
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
    GeekJobRuJobsSourceConfig(_common_filters),
    RemoteOkJobsSourceConfig(_common_filters),
    WelcomeToTheJungleJobsSourceConfig(_common_filters),

    TelegramChannelJobsSourceConfig("flutter_dart_jobs_vakansii", _no_filters),

    TelegramChannelJobsSourceConfig("evacuatejobs", _common_filters),
    TelegramChannelJobsSourceConfig(entity=-1002495603766, filters=_common_filters, forum_id=5),

    TelegramChannelJobsSourceConfig("mobile_jobs", _filters_for_groups_with_cv),
]
