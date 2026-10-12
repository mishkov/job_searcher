import asyncio
import hashlib
import math
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup

from src.job import Job
from src.job_sources.job_source import JobsSource, JobsSourceConfig
from src.search_filter import SearchFilter


class RemoteOkFetchError(RuntimeError):
    pass


class RemoteOkJobsSourceConfig(JobsSourceConfig):
    def __init__(self, filters: SearchFilter):
        super().__init__('Remote OK', filters)


class RemoteOkJobsSource(JobsSource[RemoteOkJobsSourceConfig]):
    API_URL = 'https://remoteok.com/api'

    async def fetch(self, since: datetime) -> list[Job]:
        return await asyncio.to_thread(self._fetch, since)

    def _fetch(self, since: datetime) -> list[Job]:
        if not self.config.filters.keywords:
            return []
        try:
            response = requests.get(self.API_URL, timeout=15)
            response.raise_for_status()
        except requests.HTTPError as error:
            status = error.response.status_code if error.response is not None else 'unknown'
            message = f'Remote OK API returned HTTP {status}'
            if status == 429:
                message += '; rate limit exceeded, try again later'
            raise RemoteOkFetchError(message) from None
        except requests.RequestException:
            raise RemoteOkFetchError('Remote OK API request failed; check network connectivity') from None

        try:
            records = response.json()
            if not isinstance(records, list) or not all(isinstance(record, dict) for record in records):
                raise ValueError('Expected job objects')
            # The official feed starts with update metadata and attribution terms.
            if records and 'id' not in records[0] and (
                'legal' in records[0] or 'last_updated' in records[0]
            ):
                records = records[1:]
            return self._parse_jobs(records, since)
        except (ValueError, TypeError, KeyError, OverflowError, OSError):
            raise RemoteOkFetchError('Remote OK API returned invalid job data') from None

    def _parse_jobs(self, records: list[dict], since: datetime) -> list[Job]:
        cutoff = since.replace(tzinfo=timezone.utc) if since.tzinfo is None else since
        jobs: list[Job] = []
        seen: set[str] = set()

        for record in records:
            if self._posted_at(record) < cutoff:
                continue
            for field in ('position', 'description', 'company', 'location'):
                if record.get(field) is not None and not isinstance(record[field], str):
                    raise ValueError(f'Invalid {field}')
            tags = record.get('tags')
            if tags is None:
                tags = []
            if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
                raise ValueError('Invalid tags')
            title = record.get('position') or '-'
            description = BeautifulSoup(record.get('description') or '', 'html.parser').get_text('\n', strip=True)
            details = []
            if record.get('company'):
                details.append(f"Company: {record['company']}")
            if record.get('location'):
                details.append(f"Location: {record['location']}")
            if tags:
                details.append(f"Tags: {', '.join(tags)}")
            details.append(description)
            text = '\n'.join(details)
            if not self._matches_filters(f'{title}\n{text}'):
                continue

            identity = record['id']
            if type(identity) not in (str, int) or not str(identity).strip():
                raise ValueError('Missing job identity')
            url = record['url']
            if not isinstance(url, str):
                raise ValueError('Missing job URL')
            parts = urlsplit(url)
            if parts.scheme not in ('http', 'https') or parts.netloc.lower() not in (
                'remoteok.com', 'www.remoteok.com'
            ) or not parts.path.startswith('/remote-jobs/'):
                raise ValueError('Invalid Remote OK listing URL')
            fingerprint = hashlib.sha256(f'remoteok.com:{identity}'.encode()).hexdigest()
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            jobs.append(Job(
                title=title,
                # Keep attribution inside Telegram's truncated description preview.
                description=f'Source: Remote OK\n{text}',
                salary=self._salary(record),
                is_remote_allowed=True,
                # API terms require linking to the Remote OK listing, not apply_url.
                url=url,
                fingerprint=fingerprint,
            ))
        return jobs

    def _posted_at(self, record: dict) -> datetime:
        if record.get('epoch') is not None:
            return datetime.fromtimestamp(float(record['epoch']), tz=timezone.utc)
        posted_at = datetime.fromisoformat(record['date'])
        return posted_at.replace(tzinfo=timezone.utc) if posted_at.tzinfo is None else posted_at

    def _salary(self, record: dict) -> str:
        bounds = []
        for field in ('salary_min', 'salary_max'):
            value = record.get(field)
            if value is None or value == '':
                bounds.append(None)
                continue
            if isinstance(value, bool):
                raise ValueError('Invalid salary')
            amount = float(value)
            if not math.isfinite(amount) or amount < 0:
                raise ValueError('Invalid salary')
            # The feed uses zero for undisclosed salary bounds.
            bounds.append(amount or None)
        minimum, maximum = bounds
        if minimum is None and maximum is None:
            return '-'
        if minimum is None:
            return f'up to {maximum:,.15g}'
        if maximum is None:
            return f'from {minimum:,.15g}'
        if minimum == maximum:
            return f'{minimum:,.15g}'
        return f'{minimum:,.15g} - {maximum:,.15g}'

    def _matches_filters(self, text: str) -> bool:
        text = text.casefold()
        filters = self.config.filters
        if any(keyword.casefold() in text for keyword in filters.exclude_keywords):
            return False
        return '*' in filters.keywords or any(keyword.casefold() in text for keyword in filters.keywords)
