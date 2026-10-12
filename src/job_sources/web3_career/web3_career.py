import asyncio
import hashlib
import os
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from src.job import Job
from src.job_sources.job_source import JobsSource, JobsSourceConfig
from src.search_filter import SearchFilter


class Web3CareerFetchError(RuntimeError):
    pass


class Web3CareerJobsSourceConfig(JobsSourceConfig):
    def __init__(self, filters: SearchFilter, *, api_token: str | None = None):
        super().__init__('web3.career', filters)
        self.api_token = api_token


class Web3CareerJobsSource(JobsSource[Web3CareerJobsSourceConfig]):
    API_URL = 'https://web3.career/api/v1'

    async def fetch(self, since: datetime) -> list[Job]:
        return await asyncio.to_thread(self._fetch, since)

    def _fetch(self, since: datetime) -> list[Job]:
        # Read the environment at fetch time, after main.py has loaded .env.
        token = self.config.api_token or os.environ.get('WEB3_CAREER_API_TOKEN')
        if not token or not token.strip():
            raise Web3CareerFetchError('Set WEB3_CAREER_API_TOKEN in .env to enable the Web3.career API')

        try:
            response = requests.get(
                self.API_URL,
                params={'token': token, 'tag': 'flutter', 'limit': 100, 'show_description': 'true'},
                timeout=15,
            )
            response.raise_for_status()
        except requests.HTTPError as error:
            status = error.response.status_code if error.response is not None else 'unknown'
            message = f'Web3.career API returned HTTP {status}'
            if status in (401, 403):
                message += '; check the API token and account access'
            elif status == 429:
                message += '; rate limit exceeded, try again later'
            # Requests exceptions include the token-bearing URL. Never forward it.
            raise Web3CareerFetchError(message) from None
        except requests.RequestException:
            raise Web3CareerFetchError('Web3.career API request failed; check network connectivity') from None

        try:
            records = self._extract_records(response.json())
            return self._parse_jobs(records, since)
        except (ValueError, TypeError, KeyError, OverflowError, OSError):
            raise Web3CareerFetchError('Web3.career API returned invalid job data') from None

    def _extract_records(self, data) -> list[dict]:
        if not isinstance(data, list):
            raise ValueError('Expected an array')
        # The API returns metadata strings followed by a nested jobs array.
        records = next((item for item in data if isinstance(item, list)), data)
        if not all(isinstance(record, dict) for record in records):
            raise ValueError('Expected job objects')
        return records

    def _parse_jobs(self, records: list[dict], since: datetime) -> list[Job]:
        cutoff = since.replace(tzinfo=timezone.utc) if since.tzinfo is None else since
        jobs: list[Job] = []
        seen_fingerprints: set[str] = set()

        for record in records:
            if self._posted_at(record) < cutoff:
                continue

            title = record.get('title') or '-'
            description = BeautifulSoup(record.get('description') or '', 'html.parser').get_text('\n', strip=True)
            tags = ' '.join(record.get('tags') or [])
            if not self._matches_filters(f'{title}\n{description}\n{tags}'):
                continue

            # Preserve the provider's application link, including its tracking.
            url = record['apply_url']
            if not isinstance(url, str) or urlsplit(url).scheme not in ('http', 'https') or not urlsplit(url).netloc:
                raise ValueError('Missing application URL')

            if record.get('url'):
                identity = record['url']
            elif record.get('id') is not None:
                identity = f"web3.career:{record['id']}"
            else:
                parts = urlsplit(url)
                identity = urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))
            if not isinstance(identity, str):
                raise ValueError('Invalid job identity')
            fingerprint = hashlib.sha256(identity.encode()).hexdigest()
            if fingerprint in seen_fingerprints:
                continue
            seen_fingerprints.add(fingerprint)

            remote = record.get('is_remote', record.get('remote'))
            jobs.append(Job(
                title=title,
                description=description,
                salary=self._salary(record),
                is_remote_allowed=remote if isinstance(remote, bool) else None,
                url=url,
                fingerprint=fingerprint,
            ))

        return jobs

    def _posted_at(self, record: dict) -> datetime:
        if record.get('date_epoch') is not None:
            return datetime.fromtimestamp(float(record['date_epoch']), tz=timezone.utc)

        value = record.get('postedAt') or record['date']
        try:
            posted_at = datetime.fromisoformat(value)
        except ValueError:
            posted_at = parsedate_to_datetime(value)
        return posted_at.replace(tzinfo=timezone.utc) if posted_at.tzinfo is None else posted_at

    def _salary(self, record: dict) -> str:
        if record.get('salary'):
            return str(record['salary'])

        minimum = record.get('salary_min_value')
        maximum = record.get('salary_max_value')
        estimated = minimum is None and maximum is None
        if estimated:
            minimum = record.get('estimated_min_salary')
            maximum = record.get('estimated_max_salary')

        if minimum is None and maximum is None:
            return '-'

        amounts = [f'{float(value):,.0f}' for value in (minimum, maximum) if value is not None]
        amount = ' - '.join(dict.fromkeys(amounts))
        currency = record.get('salary_currency') if not estimated else None
        unit = record.get('salary_unit') if not estimated else None
        salary = f'{currency} {amount}' if currency else amount
        if unit:
            salary += f' / {str(unit).lower()}'
        return salary + ' (estimated)' if estimated else salary

    def _matches_filters(self, text: str) -> bool:
        text = text.casefold()
        filters = self.config.filters
        if any(keyword.casefold() in text for keyword in filters.exclude_keywords):
            return False
        return '*' in filters.keywords or any(keyword.casefold() in text for keyword in filters.keywords)
