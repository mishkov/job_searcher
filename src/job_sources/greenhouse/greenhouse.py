import asyncio
import hashlib
import re
from datetime import datetime, timezone
from html import unescape
from urllib.parse import urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from src.error_reporting import posting_context, update_error_context
from src.job import Job
from src.job_sources.job_source import JobsSource, JobsSourceConfig
from src.search_filter import SearchFilter


class GreenhouseFetchError(RuntimeError):
    pass


class GreenhouseJobsSourceConfig(JobsSourceConfig):
    def __init__(self, company_slug: str, filters: SearchFilter):
        if not isinstance(company_slug, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', company_slug):
            raise ValueError('Greenhouse company_slug must be a board slug, such as "capitalontap"')
        super().__init__(f'Greenhouse: {company_slug}', filters)
        self.company_slug = company_slug

    @property
    def history_identity(self) -> str:
        return f'greenhouse:{self.company_slug}'


class GreenhouseJobsSource(JobsSource[GreenhouseJobsSourceConfig]):
    BASE_URL = 'https://boards-api.greenhouse.io/v1/boards'

    async def fetch(self, since: datetime) -> list[Job]:
        return await asyncio.to_thread(self._fetch, since)

    def _fetch(self, since: datetime) -> list[Job]:
        filters = self.config.filters
        if not filters.keywords:
            return []
        cutoff = since.replace(tzinfo=timezone.utc) if since.tzinfo is None else since
        update_error_context(operation='fetch_listing', page=0, posting=None)
        data = self._request_jobs()
        jobs, seen = [], set()
        try:
            update_error_context(operation='parse_listing')
            if not isinstance(data, dict) or not isinstance(data['jobs'], list):
                raise ValueError('Missing jobs')
            records = data['jobs']
            total = data['meta']['total']
            if type(total) is not int or total != len(records):
                raise ValueError('Incomplete listing')
            for record in records:
                update_error_context(operation='parse_posting', posting=posting_context(record))
                if not isinstance(record, dict) or type(record['id']) is not int or record['id'] <= 0:
                    raise ValueError('Invalid job ID')
                identity = str(record['id'])
                if identity in seen:
                    continue
                seen.add(identity)
                # The API lists all published jobs; their order is not chronological.
                posted = record.get('first_published') or record['updated_at']
                posted_at = datetime.fromisoformat(self._text(posted))
                if posted_at.tzinfo is None:
                    posted_at = posted_at.replace(tzinfo=timezone.utc)
                if posted_at < cutoff:
                    continue
                job = self._parse_job(record, identity)
                text = f'{job.title}\n{job.description}'.casefold()
                if any(word.casefold() in text for word in filters.exclude_keywords):
                    continue
                if '*' not in filters.keywords and not any(word.casefold() in text for word in filters.keywords):
                    continue
                jobs.append(job)
        except (ValueError, TypeError, KeyError, OverflowError):
            raise GreenhouseFetchError('Greenhouse returned invalid job data') from None
        return jobs

    def _request_jobs(self):
        try:
            response = requests.get(f'{self.BASE_URL}/{self.config.company_slug}/jobs',
                                    params={'content': 'true'}, timeout=30)
            response.raise_for_status()
        except requests.HTTPError as error:
            status = error.response.status_code if error.response is not None else 'unknown'
            message = f'Greenhouse returned HTTP {status}'
            if status == 429:
                message += '; rate limit exceeded, try again later'
            raise GreenhouseFetchError(message) from None
        except requests.RequestException:
            raise GreenhouseFetchError('Greenhouse request failed; check network connectivity') from None
        try:
            return response.json()
        except ValueError:
            raise GreenhouseFetchError('Greenhouse returned invalid job data') from None

    @staticmethod
    def _text(value) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError('Expected nonempty text')
        return value.strip()

    def _parse_job(self, record: dict, identity: str) -> Job:
        title = self._text(record['title'])
        content = self._text(record['content'])
        # Greenhouse descriptions may contain one or two layers of HTML entities.
        for _ in range(2):
            content = unescape(content)
        body = BeautifulSoup(content, 'html.parser')
        for tag in body.select('script, style'):
            tag.decompose()
        parts = [self._text(body.get_text('\n', strip=True))]
        parts.append(f"Company: {self._text(record.get('company_name') or self.config.company_slug)}")
        location = self._text(record['location']['name'])
        parts.append(f'Location: {location}')
        departments = record.get('departments', [])
        if not isinstance(departments, list):
            raise ValueError('Invalid departments')
        if departments:
            parts.append(f"Department: {', '.join(self._text(item['name']) for item in departments)}")
        url = urlsplit(self._text(record['absolute_url']))
        if url.scheme not in ('https', 'http') or not url.hostname or url.username or url.password:
            raise ValueError('Invalid job URL')
        # Custom career sites use query parameters such as gh_jid to identify jobs.
        return Job(title=title, description='\n\n'.join(parts), salary='-',
                   is_remote_allowed=True if re.search(r'\bremote\b', location, re.IGNORECASE) else None,
                   url=urlunsplit((url.scheme, url.netloc, url.path, url.query, '')),
                   fingerprint=hashlib.sha256(
                       f'greenhouse.io:{self.config.company_slug}:{identity}'.encode()).hexdigest())
