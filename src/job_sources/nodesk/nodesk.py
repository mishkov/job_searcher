import asyncio
import hashlib
import math
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from src.job import Job
from src.job_sources.job_source import JobsSource, JobsSourceConfig
from src.job_sources.nodesk.api import NoDeskApi, NoDeskFetchError
from src.search_filter import SearchFilter


class NoDeskJobsSourceConfig(JobsSourceConfig):
    def __init__(self, filters: SearchFilter):
        super().__init__('NoDesk', filters)


class NoDeskJobsSource(JobsSource[NoDeskJobsSourceConfig]):
    BASE_URL = 'https://nodesk.co/'
    PAGE_SIZE = 90

    def __init__(self, config: NoDeskJobsSourceConfig, api: NoDeskApi):
        super().__init__(config)
        self._api = api

    async def fetch(self, since: datetime) -> list[Job]:
        return await asyncio.to_thread(self._fetch, since)

    def _fetch(self, since: datetime) -> list[Job]:
        if not self.config.filters.keywords:
            return []
        cutoff = since.replace(tzinfo=timezone.utc) if since.tzinfo is None else since
        jobs: list[Job] = []
        seen: set[str] = set()
        page = 0
        while True:
            payload = self._api.get_jobs_page(page, self.PAGE_SIZE)
            try:
                records, page_count = self._extract_page(payload, page)
                for record in records:
                    published_at = datetime.fromisoformat(record['datePublished'])
                    if published_at.tzinfo is None:
                        published_at = published_at.replace(tzinfo=timezone.utc)
                    if published_at < cutoff:
                        continue
                    identity = record['objectID']
                    if not isinstance(identity, str) or not identity.strip():
                        raise ValueError('Missing job identity')
                    if identity in seen:
                        continue
                    seen.add(identity)
                    url = self._listing_url(record)
                    details = None
                    if urlsplit(url).hostname in ('nodesk.co', 'www.nodesk.co'):
                        details = self._api.get_job_posting(url)
                        if details is None:
                            continue
                    # Sponsored external vacancies retain their supplied copy and URL.
                    # is_ad never determines whether a vacancy is included.
                    job = self._parse_job(record, url, details)
                    if self._matches_filters(f'{job.title}\n{job.description}'):
                        jobs.append(job)
            except (ValueError, TypeError, KeyError, OverflowError):
                raise NoDeskFetchError('NoDesk returned invalid job data') from None

            # Results are ranked, not chronological; old jobs cannot end pagination.
            if page + 1 >= page_count:
                break
            page += 1
        return jobs

    def _extract_page(self, payload, page: int) -> tuple[list[dict], int]:
        if not isinstance(payload, dict) or not isinstance(payload.get('results'), list):
            raise ValueError('Expected search results')
        if len(payload['results']) != 1 or not isinstance(payload['results'][0], dict):
            raise ValueError('Expected one search result')
        result = payload['results'][0]
        records = result['hits']
        if not isinstance(records, list) or not all(isinstance(record, dict) for record in records):
            raise ValueError('Expected job objects')
        count = result['nbPages']
        total = result['nbHits']
        if (type(count) is not int or count < 0 or count > 100
                or type(total) is not int or total < 0
                or total > count * self.PAGE_SIZE
                or type(result['page']) is not int or result['page'] != page
                or (total and (count <= page or not records))
                or (not total and (records or count > 1))):
            raise ValueError('Invalid pagination metadata')
        return records, count

    def _listing_url(self, record: dict) -> str:
        value = record.get('permalink') or record.get('ctaUrl')
        if not isinstance(value, str) or not value.strip():
            raise ValueError('Missing job URL')
        url = urljoin(self.BASE_URL, value)
        parts = urlsplit(url)
        if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
            raise ValueError('Invalid job URL')
        if parts.hostname in ('nodesk.co', 'www.nodesk.co'):
            segments = parts.path.strip('/').split('/')
            if len(segments) != 2 or segments[0] != 'remote-jobs':
                raise ValueError('Invalid NoDesk listing URL')
        return url

    def _names(self, record: dict, field: str) -> list[str]:
        values = record.get(field) or []
        if not isinstance(values, list) or not all(
            isinstance(value, dict) and isinstance(value.get('name'), str) for value in values
        ):
            raise ValueError(f'Invalid {field}')
        return [value['name'] for value in values if value['name']]

    def _parse_job(self, record: dict, url: str, details: dict | None) -> Job:
        title = record['title']
        if not isinstance(title, str) or not title.strip():
            raise ValueError('Missing job title')
        if details:
            detail_title = details.get('title')
            if detail_title is not None and not isinstance(detail_title, str):
                raise ValueError('Invalid detail title')
            title = detail_title or title
        description = details['description'] if details else record.get('description') or ''
        if not isinstance(description, str):
            raise ValueError('Invalid description')
        sections = [BeautifulSoup(description, 'html.parser').get_text('\n', strip=True)]
        company = record.get('company') or {}
        if not isinstance(company, dict) or company.get('name') is not None and not isinstance(company['name'], str):
            raise ValueError('Invalid company')
        if company.get('name'):
            sections.append(f"Company: {company['name']}")
        for field, label in (('applicantLocations', 'Location'), ('employmentTypes', 'Contract'),
                             ('keywords', 'Tags'), ('badges', 'Details')):
            names = self._names(record, field)
            if names:
                sections.append(f"{label}: {', '.join(names)}")
        return Job(
            title=title,
            description='\n'.join(section for section in sections if section),
            salary=self._salary(record, details),
            is_remote_allowed=True,
            url=url,
            fingerprint=hashlib.sha256(f"nodesk.co:{record['objectID']}".encode()).hexdigest(),
        )

    def _salary(self, record: dict, details: dict | None) -> str:
        salary = details.get('baseSalary') if details else None
        if not salary:
            value = record.get('baseSalary')
            if value is None or value is False or value == '':
                return '-'
            if not isinstance(value, str):
                raise ValueError('Invalid salary')
            return value
        if not isinstance(salary, dict) or not isinstance(salary.get('value'), dict):
            raise ValueError('Invalid structured salary')
        value = salary['value']
        amounts = []
        for amount in (value.get('minValue', value.get('value')), value.get('maxValue', value.get('value'))):
            if amount is None:
                amounts.append(None)
            elif isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(amount) or amount < 0:
                raise ValueError('Invalid salary amount')
            else:
                amounts.append(f'{amount:,.15g}')
        minimum, maximum = amounts
        if minimum is None and maximum is None:
            return '-'
        text = (f'up to {maximum}' if minimum is None else f'from {minimum}' if maximum is None
                else minimum if minimum == maximum else f'{minimum} - {maximum}')
        currency, period = salary.get('currency'), value.get('unitText')
        if any(item is not None and not isinstance(item, str) for item in (currency, period)):
            raise ValueError('Invalid salary currency or period')
        if currency:
            text = f'{currency} {text}'
        if period:
            text += f' / {period.lower()}'
        return text

    def _matches_filters(self, text: str) -> bool:
        text = text.casefold()
        filters = self.config.filters
        if any(keyword.casefold() in text for keyword in filters.exclude_keywords):
            return False
        return '*' in filters.keywords or any(keyword.casefold() in text for keyword in filters.keywords)
