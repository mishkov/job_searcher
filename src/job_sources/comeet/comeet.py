import asyncio
import hashlib
import json
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from src.error_reporting import posting_context, update_error_context
from src.job import Job
from src.job_sources.job_source import JobsSource, JobsSourceConfig
from src.search_filter import SearchFilter


class ComeetFetchError(RuntimeError):
    pass


class ComeetJobsSourceConfig(JobsSourceConfig):
    def __init__(self, company_slug: str, company_uid: str, filters: SearchFilter):
        if not isinstance(company_slug, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', company_slug):
            raise ValueError('Comeet company_slug must be a board slug, such as "biocatch"')
        if not isinstance(company_uid, str) or not re.fullmatch(r'[A-Za-z0-9]+\.[A-Za-z0-9]+', company_uid):
            raise ValueError('Comeet company_uid must be the ID from the board URL, such as "03.00E"')
        super().__init__(f'Comeet: {company_slug}', filters)
        self.company_slug = company_slug
        self.company_uid = company_uid

    @property
    def history_identity(self) -> str:
        return f'comeet:{self.company_slug}:{self.company_uid}'


class ComeetJobsSource(JobsSource[ComeetJobsSourceConfig]):
    BASE_URL = 'https://www.comeet.com/jobs'

    async def fetch(self, since: datetime) -> list[Job]:
        return await asyncio.to_thread(self._fetch, since)

    def _fetch(self, since: datetime) -> list[Job]:
        filters = self.config.filters
        if not filters.keywords:
            return []
        cutoff = since.replace(tzinfo=timezone.utc) if since.tzinfo is None else since
        update_error_context(operation='fetch_listing', page=0, posting=None)
        html = self._request_html()
        jobs, seen = [], set()
        try:
            update_error_context(operation='parse_listing')
            soup = BeautifulSoup(html, 'html.parser')
            company = self._embedded_json(soup, 'COMPANY_DATA')
            records = self._embedded_json(soup, 'COMPANY_POSITIONS_DATA')
            if (not isinstance(company, dict) or company['slug'] != self.config.company_slug
                    or company['company_uid'] != self.config.company_uid or not isinstance(records, list)):
                raise ValueError('Invalid company listing')
            company_name = self._text(company['name'])
            for record in records:
                update_error_context(operation='parse_posting', posting=posting_context(record))
                identity = self._text(record['uid'])
                if not re.fullmatch(r'[A-Za-z0-9]+\.[A-Za-z0-9]+(?:-[A-Za-z0-9]+\.[A-Za-z0-9]+)?', identity):
                    raise ValueError('Invalid position UID')
                update_error_context(posting=posting_context({'id': identity, 'name': record.get('name')}))
                if identity in seen:
                    continue
                seen.add(identity)
                internal = record.get('is_internal', False)
                if type(internal) is not bool:
                    raise ValueError('Invalid internal flag')
                if internal:
                    continue
                # Hosted boards expose update dates, rather than publication dates.
                updated = datetime.fromisoformat(self._text(record['time_updated']))
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
                if updated < cutoff:
                    continue
                job = self._parse_job(record, identity, company_name)
                text = f'{job.title}\n{job.description}'.casefold()
                if any(word.casefold() in text for word in filters.exclude_keywords):
                    continue
                if '*' not in filters.keywords and not any(word.casefold() in text for word in filters.keywords):
                    continue
                jobs.append(job)
        except (ValueError, TypeError, KeyError, OverflowError):
            raise ComeetFetchError('Comeet returned invalid job data') from None
        return jobs

    def _request_html(self) -> str:
        try:
            response = requests.get(f'{self.BASE_URL}/{self.config.company_slug}/{self.config.company_uid}', timeout=30)
            response.raise_for_status()
            return response.text
        except requests.HTTPError as error:
            status = error.response.status_code if error.response is not None else 'unknown'
            message = f'Comeet returned HTTP {status}'
            if status == 429:
                message += '; rate limit exceeded, try again later'
            raise ComeetFetchError(message) from None
        except requests.RequestException:
            raise ComeetFetchError('Comeet request failed; check network connectivity') from None

    @staticmethod
    def _embedded_json(soup: BeautifulSoup, name: str):
        for script in soup.find_all('script', src=False):
            text = script.get_text()
            match = re.search(r'(?:^|[;\n])\s*(?:var\s+)?' + re.escape(name) + r'\s*=\s*', text)
            if match:
                # Decode only the JSON value; never execute the surrounding JavaScript.
                return json.JSONDecoder().raw_decode(text[match.end():])[0]
        raise ValueError('Missing company data')

    @staticmethod
    def _text(value) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError('Expected nonempty text')
        return value.strip()

    def _parse_job(self, record: dict, identity: str, company_name: str) -> Job:
        title = self._text(record['name'])
        details = record['custom_fields']['details']
        if not isinstance(details, list):
            raise ValueError('Invalid description sections')
        parts = []
        for section in details:
            content = section['value']
            if content is None or content == '':
                continue
            body = BeautifulSoup(self._text(content), 'html.parser')
            for tag in body.select('script, style'):
                tag.decompose()
            text = body.get_text('\n', strip=True)
            if text:
                parts.append(f"{self._text(section['name'])}\n{text}")
        if not parts:
            raise ValueError('Missing description')
        parts.append(f'Company: {company_name}')
        location = record.get('location')
        if location is not None and not isinstance(location, dict):
            raise ValueError('Invalid location')
        if location and location.get('name'):
            parts.append(f"Location: {self._text(location['name'])}")
        for field, label in (('department', 'Department'), ('employment_type', 'Employment'),
                             ('experience_level', 'Experience'), ('workplace_type', 'Workplace')):
            if record.get(field):
                parts.append(f'{label}: {self._text(record[field])}')
        workplace = record.get('workplace_type')
        remote = True if workplace in ('Remote', 'Hybrid') else False if workplace == 'On-site' else None
        if remote is None and location:
            remote = location.get('is_remote')
            if remote is not None and type(remote) is not bool:
                raise ValueError('Invalid remote flag')
        url = urlsplit(self._text(record['url_comeet_hosted_page']))
        prefix = f'/jobs/{self.config.company_slug}/{self.config.company_uid}/'
        if (url.scheme != 'https' or url.netloc != 'www.comeet.com'
                or not url.path.startswith(prefix) or url.path.rstrip('/').rsplit('/', 1)[-1] != identity):
            raise ValueError('Invalid job URL')
        return Job(title=title, description='\n\n'.join(parts), salary='-', is_remote_allowed=remote,
                   url=urlunsplit((url.scheme, url.netloc, url.path, '', '')),
                   fingerprint=hashlib.sha256(
                       f'comeet.com:{self.config.company_uid}:{identity}'.encode()).hexdigest())
