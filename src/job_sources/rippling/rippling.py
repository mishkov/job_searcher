import asyncio
import hashlib
import json
import re
from datetime import datetime, timezone
from uuid import UUID

import requests
from bs4 import BeautifulSoup

from src.error_reporting import posting_context, update_error_context
from src.job import Job
from src.job_sources.job_source import JobsSource, JobsSourceConfig
from src.search_filter import SearchFilter


class RipplingFetchError(RuntimeError):
    pass


class RipplingJobsSourceConfig(JobsSourceConfig):
    def __init__(self, company_slug: str, filters: SearchFilter):
        if not isinstance(company_slug, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', company_slug):
            raise ValueError('Rippling company_slug must be a board slug, such as "sparq"')
        super().__init__(f'Rippling: {company_slug}', filters)
        self.company_slug = company_slug

    @property
    def history_identity(self) -> str:
        return f'rippling:{self.company_slug}'


class RipplingJobsSource(JobsSource[RipplingJobsSourceConfig]):
    BASE_URL = 'https://ats.rippling.com'
    PAGE_SIZE = 100

    async def fetch(self, since: datetime) -> list[Job]:
        return await asyncio.to_thread(self._fetch, since)

    def _fetch(self, since: datetime) -> list[Job]:
        filters = self.config.filters
        if not filters.keywords:
            return []
        cutoff = since.replace(tzinfo=timezone.utc) if since.tzinfo is None else since
        slug = self.config.company_slug
        board_url = f'{self.BASE_URL}/en-US/{slug}/jobs'
        jobs, seen = [], set()
        page = 0
        try:
            while True:
                update_error_context(operation='fetch_listing', page=page, posting=None)
                props = self._request_props(board_url, params={'page': page, 'pageSize': self.PAGE_SIZE})
                update_error_context(operation='parse_listing')
                self._validate_board(props['apiData']['jobBoard'])
                queries = props['dehydratedState']['queries']
                listings = [query['state']['data'] for query in queries
                            if query['queryKey'][:3] == ['board', slug, 'job-posts']]
                if len(listings) != 1:
                    raise ValueError('Missing listing data')
                listing = listings[0]
                records = listing['items']
                total, pages, size = (listing[key] for key in ('totalItems', 'totalPages', 'pageSize'))
                if (not isinstance(records, list)
                        or any(type(value) is not int or value < 0 for value in (total, pages, size))
                        or size == 0 or listing['page'] != page
                        or pages != (total + size - 1) // size
                        or len(records) != min(size, max(0, total - page * size))):
                    raise ValueError('Incomplete listing page')
                seen_count = len(seen)
                for record in records:
                    update_error_context(operation='parse_posting', posting=posting_context(record))
                    identity = self._identity(record['id'])
                    if identity in seen:
                        continue
                    seen.add(identity)
                    # Construct URLs from the configured board and stable UUID, not tracking links.
                    url = f'{self.BASE_URL}/{slug}/jobs/{identity}'
                    update_error_context(operation='fetch_posting', posting=posting_context({
                        'id': identity, 'title': record.get('name'), 'url': url,
                    }))
                    detail = self._request_props(f'{board_url}/{identity}', skip_missing=True)
                    if detail is None:
                        continue
                    update_error_context(operation='parse_posting')
                    data = detail['apiData']
                    self._validate_board(data['jobBoard'])
                    posting = data['jobPost']
                    if self._identity(posting['uuid']) != identity:
                        raise ValueError('Mismatched posting')
                    posted_at = datetime.fromisoformat(self._text(posting['createdOn']))
                    if posted_at.tzinfo is None:
                        posted_at = posted_at.replace(tzinfo=timezone.utc)
                    if posted_at < cutoff:
                        continue
                    job = self._parse_job(posting, record, data['jobBoard'], url, identity)
                    text = f'{job.title}\n{job.description}'.casefold()
                    if any(word.casefold() in text for word in filters.exclude_keywords):
                        continue
                    if '*' not in filters.keywords and not any(word.casefold() in text for word in filters.keywords):
                        continue
                    jobs.append(job)
                if records and len(seen) == seen_count:
                    raise ValueError('Repeated listing page')
                page += 1
                if page >= pages:
                    break
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            raise RipplingFetchError('Rippling returned invalid job data') from None
        return jobs

    def _request_props(self, url: str, *, params: dict | None = None, skip_missing: bool = False):
        try:
            response = requests.get(url, params=params, timeout=30)
            if skip_missing and response.status_code in (404, 410):
                return None
            response.raise_for_status()
        except requests.HTTPError as error:
            status = error.response.status_code if error.response is not None else 'unknown'
            message = f'Rippling returned HTTP {status}'
            if status == 429:
                message += '; rate limit exceeded, try again later'
            raise RipplingFetchError(message) from None
        except requests.RequestException:
            raise RipplingFetchError('Rippling request failed; check network connectivity') from None
        soup = BeautifulSoup(response.text, 'html.parser')
        script = soup.find('script', id='__NEXT_DATA__')
        if script is None:
            raise RipplingFetchError('Rippling returned invalid job data')
        props = json.loads(script.string)['props']['pageProps']
        if not isinstance(props, dict):
            raise ValueError('Expected page data')
        return props

    def _validate_board(self, board: dict):
        if board['slug'] != self.config.company_slug:
            raise ValueError('Mismatched board')

    @staticmethod
    def _identity(value) -> str:
        if not isinstance(value, str):
            raise ValueError('Missing posting UUID')
        return str(UUID(value))

    @staticmethod
    def _text(value) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError('Expected nonempty text')
        return value.strip()

    def _parse_job(self, posting: dict, listing: dict, board: dict, url: str, identity: str) -> Job:
        title = self._text(posting['name'])
        description = posting['description']
        role = BeautifulSoup(self._text(description['role']), 'html.parser').get_text('\n', strip=True)
        if not role:
            raise ValueError('Missing role description')
        parts = [role]
        company_description = description.get('company')
        if company_description:
            parts.append(BeautifulSoup(self._text(company_description), 'html.parser').get_text('\n', strip=True))
        parts.append(f"Company: {self._text(board['companyName'])}")
        locations = listing['locations']
        if not isinstance(locations, list):
            raise ValueError('Invalid locations')
        names, workplace_types = [], set()
        for location in locations:
            names.append(self._text(location['name']))
            workplace = location.get('workplaceType')
            if workplace not in (None, 'REMOTE', 'HYBRID', 'ON_SITE'):
                raise ValueError('Invalid workplace type')
            workplace_types.add(workplace)
        if names:
            parts.append(f"Location: {', '.join(dict.fromkeys(names))}")
        remote = True if workplace_types & {'REMOTE', 'HYBRID'} else False if workplace_types == {'ON_SITE'} else None
        for workplace in sorted(workplace_types - {None}):
            parts.append(f"Workplace: {workplace.replace('_', ' ').title()}")
        department = listing.get('department')
        if department:
            parts.append(f"Department: {self._text(department['name'])}")
        # Pay text remains in the full description when supplied by the employer.
        return Job(title=title, description='\n\n'.join(parts), salary='-', is_remote_allowed=remote,
                   url=url, fingerprint=hashlib.sha256(
                       f'ats.rippling.com:{self.config.company_slug}:{identity}'.encode()).hexdigest())
