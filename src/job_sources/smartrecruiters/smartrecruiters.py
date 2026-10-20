import asyncio
import hashlib
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup

from src.error_reporting import posting_context, update_error_context
from src.job import Job
from src.job_sources.job_source import JobsSource, JobsSourceConfig
from src.search_filter import SearchFilter


class SmartRecruitersFetchError(RuntimeError):
    pass


class SmartRecruitersJobsSourceConfig(JobsSourceConfig):
    def __init__(self, filters: SearchFilter):
        super().__init__('SmartRecruiters', filters)


class SmartRecruitersJobsSource(JobsSource[SmartRecruitersJobsSourceConfig]):
    API_URL = 'https://jobs.smartrecruiters.com/sr-jobs/search'

    async def fetch(self, since: datetime) -> list[Job]:
        return await asyncio.to_thread(self._fetch, since)

    def _fetch(self, since: datetime) -> list[Job]:
        cutoff = since.replace(tzinfo=timezone.utc) if since.tzinfo is None else since
        keywords = self.config.filters.keywords
        queries = [''] if '*' in keywords else list(dict.fromkeys(keywords))
        jobs = []
        seen = set()
        for keyword in queries:
            update_error_context(operation='fetch_listing', keyword=keyword, posting=None)
            # Global search exposes the latest 100 hits and ignores offset.
            params = {'limit': 100}
            if keyword:
                params['keyword'] = keyword
            payload = self._request_json(self.API_URL, params=params)
            try:
                records = payload['content']
                if not isinstance(records, list):
                    raise ValueError('Expected job objects')
                for record in records:
                    update_error_context(operation='parse_posting', posting=posting_context(record))
                    if not isinstance(record, dict):
                        raise ValueError('Expected job object')
                    if self._posted_at(record) < cutoff:
                        continue
                    identity = self._text(record['id'])
                    company = self._text(record['company']['identifier'])
                    identity = f'{company}:{identity}'
                    if identity in seen:
                        continue
                    seen.add(identity)
                    url = self._job_url(record['applyUrl'])
                    detail_url = record['actions']['details']
                    if (not isinstance(detail_url, str) or not re.fullmatch(
                            r'https://api\.smartrecruiters\.com/v1/companies/[^/?#]+/postings/[0-9]+', detail_url)):
                        raise ValueError('Invalid detail URL')
                    update_error_context(operation='fetch_posting', posting=posting_context({
                        'id': record['id'], 'title': record.get('name'), 'url': url,
                    }))
                    details = self._request_json(detail_url, skip_missing=True)
                    if details is None:
                        continue
                    update_error_context(operation='parse_posting')
                    active = details.get('active')
                    if active is not None and type(active) is not bool:
                        raise ValueError('Invalid active flag')
                    if active is False or details.get('visibility') not in (None, 'PUBLIC'):
                        continue
                    if self._posted_at(details) < cutoff:
                        continue
                    job = self._parse_job(details, record, url, identity)
                    text = f'{job.title}\n{job.description}'.casefold()
                    filters = self.config.filters
                    if any(word.casefold() in text for word in filters.exclude_keywords):
                        continue
                    if '*' not in keywords and not any(word.casefold() in text for word in keywords):
                        continue
                    jobs.append(job)
            except (ValueError, TypeError, KeyError, OverflowError):
                raise SmartRecruitersFetchError('SmartRecruiters API returned invalid job data') from None
        return jobs

    def _request_json(self, url: str, *, params: dict | None = None, skip_missing: bool = False):
        try:
            response = requests.get(url, params=params, timeout=30)
            if skip_missing and response.status_code in (404, 410):
                return None
            response.raise_for_status()
        except requests.HTTPError as error:
            status = error.response.status_code if error.response is not None else 'unknown'
            message = f'SmartRecruiters API returned HTTP {status}'
            if status == 429:
                message += '; rate limit exceeded, try again later'
            raise SmartRecruitersFetchError(message) from None
        except requests.RequestException:
            raise SmartRecruitersFetchError('SmartRecruiters API request failed; check network connectivity') from None
        try:
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError('Expected object')
            return payload
        except ValueError:
            raise SmartRecruitersFetchError('SmartRecruiters API returned invalid job data') from None

    @staticmethod
    def _text(value) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError('Expected nonempty text')
        return value.strip()

    @classmethod
    def _posted_at(cls, record: dict) -> datetime:
        posted_at = datetime.fromisoformat(cls._text(record['releasedDate']))
        return posted_at.replace(tzinfo=timezone.utc) if posted_at.tzinfo is None else posted_at

    @classmethod
    def _job_url(cls, value) -> str:
        value = cls._text(value)
        parts = urlsplit(value)
        if parts.scheme != 'https' or parts.netloc != 'jobs.smartrecruiters.com' or not parts.path.strip('/'):
            raise ValueError('Invalid job URL')
        return value

    def _parse_job(self, details: dict, listing: dict, url: str, identity: str) -> Job:
        title = self._text(details['name'])
        sections = details['jobAd']['sections']
        if not isinstance(sections, dict) or not sections:
            raise ValueError('Missing job sections')
        parts = []
        # Other sections can contain structured media (for example videos).
        for field in ('companyDescription', 'jobDescription', 'qualifications', 'additionalInformation'):
            section = sections.get(field)
            if section is None:
                continue
            if not isinstance(section, dict) or not isinstance(section.get('text'), str):
                raise ValueError('Invalid job section')
            text = BeautifulSoup(section['text'], 'html.parser').get_text('\n', strip=True)
            if text:
                label = section.get('title')
                if label is not None and not isinstance(label, str):
                    raise ValueError('Invalid section title')
                parts.append(f'{label}:\n{text}' if label else text)
        if not parts:
            raise ValueError('Missing job description')
        parts.append(f"Company: {self._text(details['company']['name'])}")
        location = details.get('location')
        if location is None:
            location = listing.get('location', {})
        if not isinstance(location, dict):
            raise ValueError('Invalid location')
        location_text = location.get('fullLocation') or listing.get('shortLocation')
        if location_text:
            parts.append(f'Location: {self._text(location_text)}')
        remote, hybrid = location.get('remote'), location.get('hybrid')
        if any(value is not None and type(value) is not bool for value in (remote, hybrid)):
            raise ValueError('Invalid remote flag')
        remote_allowed = True if remote or hybrid else False if remote is False else None
        if remote or hybrid:
            parts.append('Workplace: Remote' if remote else 'Workplace: Hybrid')
        if location.get('hybridDescription'):
            parts.append(self._text(location['hybridDescription']))
        for field, label in (('typeOfEmployment', 'Employment'), ('experienceLevel', 'Experience')):
            value = details.get(field, {})
            if not isinstance(value, dict):
                raise ValueError('Invalid employment metadata')
            if value.get('label'):
                parts.append(f"{label}: {self._text(value['label'])}")
        # The public API has no structured salary field; retain any pay text in sections.
        return Job(title=title, description='\n\n'.join(parts), salary='-',
                   is_remote_allowed=remote_allowed, url=url,
                   fingerprint=hashlib.sha256(f'smartrecruiters.com:{identity}'.encode()).hexdigest())
