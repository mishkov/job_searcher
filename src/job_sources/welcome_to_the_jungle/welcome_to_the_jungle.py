import asyncio
import hashlib
from dataclasses import replace
from datetime import datetime, timezone
from urllib.parse import quote
from uuid import uuid4

import requests
from bs4 import BeautifulSoup

from src.job import Job
from src.job_sources.job_source import JobsSource, JobsSourceConfig
from src.search_filter import SearchFilter


class WelcomeToTheJungleFetchError(RuntimeError):
    pass


class WelcomeToTheJungleJobsSourceConfig(JobsSourceConfig):
    def __init__(self, filters: SearchFilter):
        super().__init__('welcometothejungle.com', filters)


class WelcomeToTheJungleJobsSource(JobsSource[WelcomeToTheJungleJobsSourceConfig]):
    API_URL = 'https://api.welcometothejungle.com/api/v3/public/jobs'
    DETAIL_API_URL = 'https://api.welcometothejungle.com/api/v1/organizations/{organization}/jobs/{job}'
    JOB_URL = 'https://www.welcometothejungle.com/en/companies/{organization}/jobs/{job}'

    async def fetch(self, since: datetime) -> list[Job]:
        return await asyncio.to_thread(self._fetch, since)

    def _fetch(self, since: datetime) -> list[Job]:
        cutoff = since.replace(tzinfo=timezone.utc) if since.tzinfo is None else since
        keywords = self.config.filters.keywords
        queries = [''] if '*' in keywords else list(dict.fromkeys(keywords))
        jobs: list[Job] = []
        seen: set[str] = set()

        for keyword in queries:
            query_id = str(uuid4())
            page = 1
            while True:
                params = {'include_empty_salary': 'true', 'page': page, 'query_id': query_id}
                if keyword:
                    params['job_title'] = keyword
                payload = self._request_json(self.API_URL, params=params)
                try:
                    records, page_count = self._extract_page(payload, page)
                    for record in records:
                        if record.get('status') != 'published':
                            continue
                        # Results are relevance-ranked, so old jobs cannot end pagination.
                        published_at = datetime.fromisoformat(record['published_at'])
                        if published_at.tzinfo is None:
                            published_at = published_at.replace(tzinfo=timezone.utc)
                        if published_at < cutoff:
                            continue
                        job = self._parse_job(record)
                        if job.fingerprint in seen:
                            continue
                        # Cache processed identities even when full-text filters reject them.
                        seen.add(job.fingerprint)
                        organization_slug, slug = self._job_slugs(record)
                        details_payload = self._request_json(
                            self.DETAIL_API_URL.format(organization=organization_slug, job=slug),
                            skip_missing=True,
                        )
                        if details_payload is None:
                            continue
                        details = details_payload['job']
                        if not isinstance(details, dict):
                            raise ValueError('Expected job details')
                        # A vacancy may be removed between the listing and detail requests.
                        if details.get('status') not in (None, 'published'):
                            continue
                        description = self._detail_description(details)
                        if description:
                            job = replace(job, description=f'{description}\n\n{job.description}')
                        if not self._matches_filters(f'{job.title}\n{job.description}'):
                            continue
                        jobs.append(job)
                except (ValueError, TypeError, KeyError, OverflowError):
                    raise WelcomeToTheJungleFetchError(
                        'Welcome to the Jungle API returned invalid job data'
                    ) from None

                if not records or page >= page_count:
                    break
                page += 1

        return jobs

    def _request_json(self, url: str, *, params: dict | None = None, skip_missing: bool = False) -> dict | None:
        try:
            response = requests.get(url, params=params, timeout=15)
            if skip_missing and response.status_code in (404, 410):
                return None
            response.raise_for_status()
        except requests.HTTPError as error:
            status = error.response.status_code if error.response is not None else 'unknown'
            message = f'Welcome to the Jungle API returned HTTP {status}'
            if status == 429:
                message += '; rate limit exceeded, try again later'
            raise WelcomeToTheJungleFetchError(message) from None
        except requests.RequestException:
            raise WelcomeToTheJungleFetchError(
                'Welcome to the Jungle API request failed; check network connectivity'
            ) from None

        try:
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError('Expected an object')
            return payload
        except ValueError:
            raise WelcomeToTheJungleFetchError(
                'Welcome to the Jungle API returned invalid job data'
            ) from None

    def _detail_description(self, details: dict) -> str:
        sections = []
        for field, label in (('description', 'Job description'), ('profile', 'Preferred experience'),
                             ('recruitment_process', 'Recruitment process')):
            value = details.get(field)
            if value is None:
                continue
            if not isinstance(value, str):
                raise ValueError(f'Invalid {field}')
            text = BeautifulSoup(value, 'html.parser').get_text('\n', strip=True)
            if text:
                sections.append(f'{label}:\n{text}')
        return '\n\n'.join(sections)

    def _extract_page(self, payload, page: int) -> tuple[list[dict], int]:
        if not isinstance(payload, dict):
            raise ValueError('Expected an object')
        records = payload['data']
        metadata = payload['metadata']
        if not isinstance(records, list) or not all(isinstance(record, dict) for record in records):
            raise ValueError('Expected job objects')
        if not isinstance(metadata, dict):
            raise ValueError('Expected pagination metadata')
        page_count = metadata['page_count']
        if type(page_count) is not int or page_count < 0 or metadata['page'] != page:
            raise ValueError('Invalid pagination metadata')
        if records and page_count < page:
            raise ValueError('Invalid page count')
        return records, page_count

    def _parse_job(self, record: dict) -> Job:
        for field in ('name', 'company_summary', 'contract_type', 'remote', 'salary_currency', 'salary_period'):
            if record.get(field) is not None and not isinstance(record[field], str):
                raise ValueError(f'Invalid {field}')
        reference = record['reference']
        if not isinstance(reference, str) or not reference.strip():
            raise ValueError('Missing job identity')
        organization_slug, slug = self._job_slugs(record)
        organization = record['organization']

        remote = record.get('remote')
        remote_allowed = {'partial': True, 'fulltime': True, 'no': False}.get(remote)
        offices = record.get('offices') or ([record['office']] if record.get('office') else [])
        if not isinstance(offices, list) or not all(isinstance(office, dict) for office in offices):
            raise ValueError('Invalid offices')
        locations = []
        for office in offices:
            if any(office.get(field) is not None and not isinstance(office[field], str)
                   for field in ('city', 'country_code')):
                raise ValueError('Invalid location')
            location = ', '.join(value for value in (office.get('city'), office.get('country_code')) if value)
            if location and location not in locations:
                locations.append(location)
        description_parts = []
        if organization.get('name'):
            description_parts.append(f"Company: {organization['name']}")
        if locations:
            description_parts.append(f"Location: {'; '.join(locations)}")
        if record.get('contract_type'):
            description_parts.append(f"Contract: {record['contract_type'].replace('_', ' ')}")
        remote_label = {'partial': 'Hybrid', 'fulltime': 'Full remote', 'no': 'On-site'}.get(remote)
        if remote_label:
            description_parts.append(f'Remote: {remote_label}')
        if record.get('company_summary'):
            description_parts.append(BeautifulSoup(record['company_summary'], 'html.parser').get_text('\n', strip=True))

        return Job(
            title=record.get('name') or '-',
            description='\n'.join(description_parts),
            salary=self._salary(record),
            is_remote_allowed=remote_allowed,
            url=self.JOB_URL.format(organization=organization_slug, job=slug),
            fingerprint=hashlib.sha256(f'welcometothejungle.com:{reference}'.encode()).hexdigest(),
        )

    def _job_slugs(self, record: dict) -> tuple[str, str]:
        slug = record['slug']
        organization = record['organization']
        if not isinstance(organization, dict):
            raise ValueError('Missing organization')
        website_organization = organization.get('website_organization') or {}
        if not isinstance(website_organization, dict):
            raise ValueError('Invalid website organization')
        organization_slug = website_organization.get('slug') or organization.get('slug')
        if not all(isinstance(value, str) and value.strip() for value in (slug, organization_slug)):
            raise ValueError('Missing job URL')
        return quote(organization_slug, safe=''), quote(slug, safe='')

    def _salary(self, record: dict) -> str:
        minimum, maximum = record.get('salary_min'), record.get('salary_max')
        if minimum is None and maximum is None:
            return '-'
        if minimum is None:
            amount = f'up to {float(maximum):,.15g}'
        elif maximum is None:
            amount = f'from {float(minimum):,.15g}'
        elif minimum == maximum:
            amount = f'{float(minimum):,.15g}'
        else:
            amount = f'{float(minimum):,.15g} - {float(maximum):,.15g}'
        currency = record.get('salary_currency')
        salary = f'{currency} {amount}' if currency else amount
        if record.get('salary_period'):
            salary += f" / {record['salary_period']}"
        return salary

    def _matches_filters(self, text: str) -> bool:
        text = text.casefold()
        filters = self.config.filters
        if any(keyword.casefold() in text for keyword in filters.exclude_keywords):
            return False
        return '*' in filters.keywords or any(keyword.casefold() in text for keyword in filters.keywords)
