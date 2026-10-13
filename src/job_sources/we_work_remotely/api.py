from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urlunsplit


CATEGORIES = (
    'remote-customer-support-jobs',
    'remote-product-jobs',
    'remote-full-stack-programming-jobs',
    'remote-back-end-programming-jobs',
    'remote-front-end-programming-jobs',
    'remote-sales-and-marketing-jobs',
    'remote-management-and-finance-jobs',
    'remote-design-jobs',
    'remote-devops-sysadmin-jobs',
    'all-other-remote-jobs',
)
FIELDS = ('title', 'description', 'link', 'guid', 'pubDate', 'expires_at',
          'region', 'country', 'state', 'skills', 'category', 'type')


class WeWorkRemotelyFetchError(RuntimeError):
    pass


@dataclass
class FeedResponse:
    # None means HTTP 304, whereas [] is a successful empty feed.
    jobs: list[dict] | None
    etag: str | None = None
    last_modified: str | None = None


class WeWorkRemotelyApi(ABC):
    @abstractmethod
    def get_feed(self, category: str, *, etag: str | None = None,
                 last_modified: str | None = None) -> FeedResponse:
        pass


def posted_at(value: str):
    date = parsedate_to_datetime(value)
    return date.replace(tzinfo=timezone.utc) if date.tzinfo is None else date


def listing_identity(url: str) -> str:
    parts = urlsplit(url)
    if (parts.scheme not in ('http', 'https')
            or parts.netloc.lower() not in ('weworkremotely.com', 'www.weworkremotely.com')
            or not parts.path.startswith('/remote-jobs/')
            or not parts.path.removeprefix('/remote-jobs/').strip('/')):
        raise ValueError('Invalid WWR listing URL')
    return urlunsplit(('https', 'weworkremotely.com', parts.path.rstrip('/'), '', ''))


def validate_jobs(jobs):
    if not isinstance(jobs, list):
        raise ValueError('Expected feed entries')
    for job in jobs:
        if not isinstance(job, dict) or any(not isinstance(job.get(field), str) for field in FIELDS):
            raise ValueError('Invalid feed entry')
        if not job['title'].strip() or not job['description'].strip():
            raise ValueError('Missing title or description')
        listing_identity(job['link'])
        posted_at(job['pubDate'])
        if job['expires_at']:
            posted_at(job['expires_at'])
    return jobs
