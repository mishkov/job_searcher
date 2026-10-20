import asyncio
import hashlib
import math
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

from src.error_reporting import posting_context, update_error_context
from src.job import Job
from src.job_sources.job_source import JobsSource, JobsSourceConfig
from src.search_filter import SearchFilter


class AndersenFetchError(RuntimeError):
    pass


class AndersenJobsSourceConfig(JobsSourceConfig):
    def __init__(self, filters: SearchFilter):
        super().__init__('Andersen', filters)


class AndersenJobsSource(JobsSource[AndersenJobsSourceConfig]):
    API_URL = 'https://asite-api.andersenlab.com/api/integration/recruitment/vacancies'
    JOB_URL = 'https://people-andersenlab.com/vacancy/{identity}'
    HEADERS = {'Accept-Language': 'en', 'Api-Version': 'v5'}

    async def fetch(self, since: datetime) -> list[Job]:
        return await asyncio.to_thread(self._fetch, since)

    def _fetch(self, since: datetime) -> list[Job]:
        filters = self.config.filters
        if not filters.keywords:
            return []
        cutoff = since.replace(tzinfo=timezone.utc) if since.tzinfo is None else since.astimezone(timezone.utc)
        update_error_context(operation='fetch_listing', posting=None)
        records = self._request_jobs()
        jobs = []
        seen = set()
        try:
            # The API returns the complete active list, including full descriptions.
            if not isinstance(records, list):
                raise ValueError('Expected job objects')
            for record in records:
                update_error_context(operation='parse_posting', posting=posting_context(record))
                if not isinstance(record, dict):
                    raise ValueError('Expected a job object')
                identity = record['vacancy_id']
                if type(identity) is not int or identity <= 0:
                    raise ValueError('Invalid job identity')
                if identity in seen:
                    continue
                seen.add(identity)
                update_error_context(posting=posting_context({
                    'id': identity, 'title': record.get('name'), 'url': self.JOB_URL.format(identity=identity),
                }))
                # Currently absent from the API. Undated active jobs must remain
                # eligible on every run; persisted seen-job history deduplicates.
                opening_date = record.get('openingDate')
                if opening_date is not None:
                    if not isinstance(opening_date, str):
                        raise ValueError('Invalid opening date')
                    posted_at = datetime.fromisoformat(opening_date)
                    if posted_at.tzinfo is None:
                        posted_at = posted_at.replace(tzinfo=timezone.utc)
                    posted_at = posted_at.astimezone(timezone.utc)
                    if (posted_at.date() < cutoff.date() if len(opening_date) == 10 else posted_at < cutoff):
                        continue
                job = self._parse_job(record, identity)
                text = f'{job.title}\n{job.description}'.casefold()
                if any(keyword.casefold() in text for keyword in filters.exclude_keywords):
                    continue
                if '*' not in filters.keywords and not any(keyword.casefold() in text for keyword in filters.keywords):
                    continue
                jobs.append(job)
        except (ValueError, TypeError, KeyError, OverflowError):
            raise AndersenFetchError('Andersen API returned invalid job data') from None
        return jobs

    def _request_jobs(self):
        try:
            response = requests.get(self.API_URL, headers=self.HEADERS, timeout=30)
            response.raise_for_status()
        except requests.HTTPError as error:
            status = error.response.status_code if error.response is not None else 'unknown'
            message = f'Andersen API returned HTTP {status}'
            if status == 429:
                message += '; rate limit exceeded, try again later'
            raise AndersenFetchError(message) from None
        except requests.RequestException:
            raise AndersenFetchError('Andersen API request failed; check network connectivity') from None
        try:
            return response.json()
        except ValueError:
            raise AndersenFetchError('Andersen API returned invalid job data') from None

    def _parse_job(self, record: dict, identity: int) -> Job:
        title = record['name']
        if not isinstance(title, str) or not title.strip():
            raise ValueError('Missing job title')
        remote = record.get('isRemote')
        worldwide = record.get('isWorldWide')
        if any(value is not None and type(value) is not bool for value in (remote, worldwide)):
            raise ValueError('Invalid remote or worldwide flag')
        sections = []
        for field, label in (('summary', 'Job description'), ('responsibilities', 'Responsibilities'),
                             ('requirements', 'Requirements'), ('niceToHave', 'Nice to have'),
                             ('joinUsReason', 'Reasons to join')):
            text = self._section_text(record.get(field))
            if text:
                sections.append(f'{label}:\n{text}')
        if not sections:
            raise ValueError('Missing job description')
        sections.append('Company: Andersen')
        level = record.get('level')
        if level is not None:
            if not isinstance(level, str):
                raise ValueError('Invalid level')
            if level:
                sections.append(f'Level: {level}')
        for field, label in (('technologies', 'Technologies'), ('specializations', 'Specializations')):
            values = record.get(field)
            if values is not None:
                if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
                    raise ValueError('Invalid technologies or specializations')
                if values:
                    sections.append(f"{label}: {', '.join(values)}")
        locations = self._locations(record.get('lookingLocations'))
        if worldwide:
            locations = ['Worldwide', *locations]
        if locations:
            sections.append(f"Location: {'; '.join(locations)}")
        excluded_locations = self._locations(record.get('notLookingLocations'))
        if excluded_locations:
            sections.append(f"Not hiring in: {'; '.join(excluded_locations)}")
        return Job(
            title=title.strip(), description='\n\n'.join(sections), salary=self._salary(record.get('salary')),
            is_remote_allowed=remote, url=self.JOB_URL.format(identity=identity),
            fingerprint=hashlib.sha256(f'people-andersenlab.com:{identity}'.encode()).hexdigest(),
        )

    @staticmethod
    def _section_text(blocks) -> str:
        if blocks is None:
            return ''
        if not isinstance(blocks, list):
            raise ValueError('Invalid description blocks')
        parts = []
        for block in blocks:
            if not isinstance(block, dict):
                raise ValueError('Invalid description block')
            content = block['content']
            if block['TYPE'] == 'text' and isinstance(content, str):
                values = [content]
            elif block['TYPE'] == 'list' and isinstance(content, list) and all(isinstance(v, str) for v in content):
                values = content
            else:
                raise ValueError('Invalid description content')
            for value in values:
                text = BeautifulSoup(value, 'html.parser').get_text('\n', strip=True)
                if text:
                    parts.append(text)
        return '\n'.join(parts)

    @staticmethod
    def _locations(locations) -> list[str]:
        if locations is None:
            return []
        if not isinstance(locations, list):
            raise ValueError('Invalid locations')
        result = []
        for location in locations:
            if not isinstance(location, dict):
                raise ValueError('Invalid location')
            country = location.get('countryName') or location.get('countryCode')
            cities = location.get('cities') or []
            if (not isinstance(country, str) or not country.strip() or not isinstance(cities, list)
                    or not all(isinstance(city, str) for city in cities)):
                raise ValueError('Invalid country or cities')
            result.append(f"{', '.join(cities)}, {country}" if cities else country)
        return result

    @staticmethod
    def _salary(salary) -> str:
        if salary is None:
            return '-'
        if not isinstance(salary, dict):
            raise ValueError('Invalid salary')
        minimum, maximum = salary.get('minSalary'), salary.get('maxSalary')
        for value in (minimum, maximum):
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
                raise ValueError('Invalid salary amount')
        currency = salary.get('currencyCode')
        if currency is not None and not isinstance(currency, str):
            raise ValueError('Invalid salary currency')
        if minimum is None and maximum is None:
            return '-'
        if minimum is not None and maximum is not None and minimum > maximum:
            raise ValueError('Invalid salary range')
        amount = (f'up to {maximum:,.15g}' if minimum is None else f'from {minimum:,.15g}' if maximum is None
                  else f'{minimum:,.15g}' if minimum == maximum else f'{minimum:,.15g} - {maximum:,.15g}')
        # The API supplies no pay period; preserve amounts without assuming one.
        return f'{currency} {amount}' if currency else amount
