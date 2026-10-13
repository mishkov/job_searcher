import re
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit


BASE_URL = 'https://relocate.me'


class RelocateMeFetchError(RuntimeError):
    pass


def listing_url(value: str) -> tuple[str, str]:
    if not isinstance(value, str) or not value.strip():
        raise ValueError('Missing job URL')
    parts = urlsplit(urljoin(BASE_URL, value))
    if (parts.scheme != 'https' or parts.netloc not in ('relocate.me', 'www.relocate.me')
            or parts.query or parts.fragment):
        raise ValueError('Invalid Relocate.me job URL')
    match = re.fullmatch(r'/[^/]+/[^/]+/[^/]+/[^/]+-([0-9]+)/?', parts.path)
    if match is None:
        raise ValueError('Invalid Relocate.me job URL')
    return BASE_URL + parts.path.rstrip('/'), match[1]


def validate_posting(posting: dict) -> None:
    if not isinstance(posting, dict):
        raise ValueError('Expected job details')
    for field in ('id', 'title', 'description', 'date_posted', 'salary'):
        value = posting.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f'Missing {field}')
    if not re.fullmatch(r'[0-9]+', posting['id']):
        raise ValueError('Invalid job identity')
    datetime.fromisoformat(posting['date_posted'])
    if 'remote' not in posting or posting['remote'] is not None and type(posting['remote']) is not bool:
        raise ValueError('Invalid remote availability')
    expiry = posting.get('valid_through')
    if expiry is not None:
        if not isinstance(expiry, str):
            raise ValueError('Invalid expiry date')
        datetime.fromisoformat(expiry)


def is_expired(expiry: str | None) -> bool:
    if expiry is None:
        return False
    expires = datetime.fromisoformat(expiry)
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    return expires.date() < now.date() if len(expiry) == 10 else expires < now


class RelocateMeApi(ABC):
    @abstractmethod
    def get_jobs_page(self, page: int) -> dict:
        """Return unfiltered listing URLs and the next page number."""
        pass

    @abstractmethod
    def get_job_posting(self, url: str) -> dict | None:
        """Return parsed details, or None for an archived or removed job."""
        pass
