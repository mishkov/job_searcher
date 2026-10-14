import asyncio
import hashlib
from datetime import datetime, timezone

from src.error_reporting import posting_context, update_error_context
from src.job import Job
from src.job_sources.job_source import JobsSource, JobsSourceConfig
from src.job_sources.relocate_me.api import (
    RelocateMeApi, RelocateMeFetchError, is_expired, listing_url, validate_posting,
)
from src.search_filter import SearchFilter


class RelocateMeJobsSourceConfig(JobsSourceConfig):
    def __init__(self, filters: SearchFilter):
        super().__init__('Relocate.me', filters)


class RelocateMeJobsSource(JobsSource[RelocateMeJobsSourceConfig]):
    def __init__(self, config: RelocateMeJobsSourceConfig, api: RelocateMeApi):
        super().__init__(config)
        self._api = api

    async def fetch(self, since: datetime) -> list[Job]:
        return await asyncio.to_thread(self._fetch, since)

    def _fetch(self, since: datetime) -> list[Job]:
        if not self.config.filters.keywords:
            return []
        cutoff = since.replace(tzinfo=timezone.utc) if since.tzinfo is None else since.astimezone(timezone.utc)
        jobs = []
        seen = set()
        page = 1
        while True:
            update_error_context(operation='fetch_listing', page=page, posting=None)
            payload = self._api.get_jobs_page(page)
            try:
                if not isinstance(payload, dict) or not isinstance(payload['urls'], list):
                    raise ValueError('Invalid listing page')
                next_page = payload['next_page']
                if next_page is not None and (type(next_page) is not int or next_page != page + 1
                                              or next_page > 100 or not payload['urls']):
                    raise ValueError('Invalid pagination')
                for value in payload['urls']:
                    update_error_context(operation='parse_posting', posting=posting_context({'url': value}))
                    url, identity = listing_url(value)
                    if identity in seen:
                        continue
                    seen.add(identity)
                    update_error_context(operation='fetch_posting')
                    posting = self._api.get_job_posting(url)
                    if posting is None:
                        continue
                    update_error_context(operation='parse_posting', posting={**posting_context(posting), 'url': url})
                    validate_posting(posting)
                    if posting['id'] != identity:
                        raise ValueError('Mismatched job identity')
                    if is_expired(posting.get('valid_through')):
                        continue
                    posted_at = datetime.fromisoformat(posting['date_posted'])
                    if posted_at.tzinfo is None:
                        posted_at = posted_at.replace(tzinfo=timezone.utc)
                    # Date-only metadata cannot distinguish jobs posted earlier today.
                    # Keep the whole cutoff day; the seen-job repository deduplicates.
                    if (posted_at.date() < cutoff.date() if len(posting['date_posted']) == 10
                            else posted_at < cutoff):
                        continue
                    text = f"{posting['title']}\n{posting['description']}".casefold()
                    filters = self.config.filters
                    if any(keyword.casefold() in text for keyword in filters.exclude_keywords):
                        continue
                    if '*' not in filters.keywords and not any(keyword.casefold() in text for keyword in filters.keywords):
                        continue
                    jobs.append(Job(
                        title=posting['title'], description=posting['description'], salary=posting['salary'],
                        is_remote_allowed=posting['remote'], url=url,
                        fingerprint=hashlib.sha256(f'relocate.me:{identity}'.encode()).hexdigest(),
                    ))
            except (ValueError, TypeError, KeyError, OverflowError):
                raise RelocateMeFetchError('Relocate.me returned invalid job data') from None
            # Listing order is not chronological; always follow the next-page link.
            if next_page is None:
                break
            page = next_page
        return jobs
