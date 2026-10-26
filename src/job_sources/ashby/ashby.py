import asyncio
import hashlib
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit
from uuid import UUID

import requests
from bs4 import BeautifulSoup

from src.error_reporting import posting_context, update_error_context
from src.job import Job
from src.job_sources.job_source import JobsSource, JobsSourceConfig
from src.search_filter import SearchFilter


class AshbyFetchError(RuntimeError):
    pass


class AshbyJobsSourceConfig(JobsSourceConfig):
    def __init__(self, company_slug: str, filters: SearchFilter):
        if not isinstance(company_slug, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', company_slug):
            raise ValueError('Ashby company_slug must be a board slug, such as "gismart"')
        super().__init__(f'Ashby: {company_slug}', filters)
        self.company_slug = company_slug

    @property
    def history_identity(self) -> str:
        return f'ashby:{self.company_slug}'


class AshbyJobsSource(JobsSource[AshbyJobsSourceConfig]):
    BASE_URL = 'https://api.ashbyhq.com/posting-api/job-board'

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
            for record in data['jobs']:
                update_error_context(operation='parse_posting', posting=posting_context(record))
                if not isinstance(record, dict) or type(record['isListed']) is not bool:
                    raise ValueError('Invalid listing visibility')
                if not record['isListed']:
                    continue
                identity, url = self._job_url(record['jobUrl'])
                if identity in seen:
                    continue
                seen.add(identity)
                # The full board is returned in one response, without chronological ordering.
                posted_at = datetime.fromisoformat(self._text(record['publishedAt']))
                if posted_at.tzinfo is None:
                    posted_at = posted_at.replace(tzinfo=timezone.utc)
                if posted_at < cutoff:
                    continue
                job = self._parse_job(record, identity, url)
                text = f'{job.title}\n{job.description}'.casefold()
                if any(word.casefold() in text for word in filters.exclude_keywords):
                    continue
                if '*' not in filters.keywords and not any(word.casefold() in text for word in filters.keywords):
                    continue
                jobs.append(job)
        except (ValueError, TypeError, KeyError, OverflowError):
            raise AshbyFetchError('Ashby returned invalid job data') from None
        return jobs

    def _request_jobs(self):
        try:
            response = requests.get(f'{self.BASE_URL}/{self.config.company_slug}',
                                    params={'includeCompensation': 'true'}, timeout=30)
            response.raise_for_status()
        except requests.HTTPError as error:
            status = error.response.status_code if error.response is not None else 'unknown'
            message = f'Ashby returned HTTP {status}'
            if status == 429:
                message += '; rate limit exceeded, try again later'
            raise AshbyFetchError(message) from None
        except requests.RequestException:
            raise AshbyFetchError('Ashby request failed; check network connectivity') from None
        try:
            return response.json()
        except ValueError:
            raise AshbyFetchError('Ashby returned invalid job data') from None

    @staticmethod
    def _text(value) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError('Expected nonempty text')
        return value.strip()

    def _job_url(self, value) -> tuple[str, str]:
        url = urlsplit(self._text(value))
        parts = url.path.strip('/').split('/')
        if (url.scheme != 'https' or url.netloc != 'jobs.ashbyhq.com' or len(parts) != 2
                or parts[0] != self.config.company_slug):
            raise ValueError('Invalid job URL')
        identity = str(UUID(parts[1]))
        return identity, f'https://jobs.ashbyhq.com/{self.config.company_slug}/{identity}'

    def _parse_job(self, record: dict, identity: str, url: str) -> Job:
        title = self._text(record['title'])
        description = record.get('descriptionPlain')
        if not description:
            body = BeautifulSoup(self._text(record['descriptionHtml']), 'html.parser')
            for tag in body.select('script, style'):
                tag.decompose()
            description = body.get_text('\n', strip=True)
        parts = [self._text(description), f'Company: {self.config.company_slug}']
        locations = []
        if record.get('location'):
            locations.append(self._text(record['location']))
        secondary = record.get('secondaryLocations', [])
        if not isinstance(secondary, list):
            raise ValueError('Invalid secondary locations')
        for location in secondary:
            locations.append(self._text(location['location']))
        if locations:
            parts.append(f"Location: {', '.join(dict.fromkeys(locations))}")
        for field, label in (('department', 'Department'), ('team', 'Team'),
                             ('employmentType', 'Employment'), ('workplaceType', 'Workplace')):
            if record.get(field):
                parts.append(f'{label}: {self._text(record[field])}')
        remote = record.get('isRemote')
        if remote is not None and type(remote) is not bool:
            raise ValueError('Invalid remote status')
        workplace = record.get('workplaceType')
        if workplace in ('Remote', 'Hybrid', 'OnSite'):
            remote = workplace != 'OnSite'
        compensation = record.get('compensation') or {}
        if not isinstance(compensation, dict):
            raise ValueError('Invalid compensation')
        salary = self._text(compensation.get('scrapeableCompensationSalarySummary') or '-')
        summary = compensation.get('compensationTierSummary')
        if summary:
            parts.append(f'Compensation: {self._text(summary)}')
        elif salary != '-':
            parts.append(f'Compensation: {salary}')
        return Job(title=title, description='\n\n'.join(parts), salary=salary,
                   is_remote_allowed=remote, url=url, fingerprint=hashlib.sha256(
                       f'ashbyhq.com:{self.config.company_slug}:{identity}'.encode()).hexdigest())
