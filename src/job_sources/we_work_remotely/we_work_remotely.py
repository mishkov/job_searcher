import asyncio
import hashlib
from datetime import datetime, timezone

from bs4 import BeautifulSoup

from src.error_reporting import posting_context, update_error_context
from src.job import Job
from src.job_sources.job_source import JobsSource, JobsSourceConfig
from src.job_sources.we_work_remotely.api import (
    CATEGORIES, WeWorkRemotelyApi, WeWorkRemotelyFetchError, listing_identity, posted_at,
)
from src.search_filter import SearchFilter


class WeWorkRemotelyJobsSourceConfig(JobsSourceConfig):
    def __init__(self, filters: SearchFilter):
        super().__init__('We Work Remotely', filters)


class WeWorkRemotelyJobsSource(JobsSource[WeWorkRemotelyJobsSourceConfig]):
    def __init__(self, config: WeWorkRemotelyJobsSourceConfig, api: WeWorkRemotelyApi):
        super().__init__(config)
        self._api = api

    async def fetch(self, since: datetime) -> list[Job]:
        return await asyncio.to_thread(self._fetch, since)

    def _fetch(self, since: datetime) -> list[Job]:
        if not self.config.filters.keywords:
            return []
        cutoff = since.replace(tzinfo=timezone.utc) if since.tzinfo is None else since
        now = datetime.now(timezone.utc)
        jobs = []
        seen = set()
        # The combined feed caps each category at ten entries; category feeds are fuller.
        for category in CATEGORIES:
            update_error_context(operation='fetch_listing', category=category, posting=None)
            response = self._api.get_feed(category)
            if response.jobs is None:
                raise WeWorkRemotelyFetchError('We Work Remotely returned an unexpected HTTP 304')
            for record in response.jobs:
                update_error_context(operation='parse_posting', posting=posting_context(record))
                if posted_at(record['pubDate']) < cutoff:
                    continue
                if record['expires_at'] and posted_at(record['expires_at']) <= now:
                    continue
                identity = listing_identity(record['link'])
                if identity in seen:
                    continue
                description = BeautifulSoup(record['description'], 'html.parser').get_text('\n', strip=True)
                details = []
                for field, label in (('region', 'Region'), ('country', 'Countries'), ('state', 'State'),
                                     ('skills', 'Skills'), ('category', 'Category'), ('type', 'Contract')):
                    if record[field]:
                        details.append(f'{label}: {record[field]}')
                text = '\n'.join([*details, description])
                if not self._matches_filters(f"{record['title']}\n{text}"):
                    continue
                seen.add(identity)
                jobs.append(Job(
                    title=record['title'],
                    description=f'Source: We Work Remotely\n{text}',
                    salary='-',  # RSS has no structured salary field; keep supplied pay in the description.
                    is_remote_allowed=True,
                    url=record['link'],
                    fingerprint=hashlib.sha256(identity.encode()).hexdigest(),
                ))
        return jobs

    def _matches_filters(self, text: str) -> bool:
        text = text.casefold()
        filters = self.config.filters
        if any(keyword.casefold() in text for keyword in filters.exclude_keywords):
            return False
        return '*' in filters.keywords or any(keyword.casefold() in text for keyword in filters.keywords)
