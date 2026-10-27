import asyncio
import hashlib
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from src.error_reporting import posting_context, update_error_context
from src.job import Job
from src.job_sources.job_source import JobsSource, JobsSourceConfig
from src.search_filter import SearchFilter


class BeteraFetchError(RuntimeError):
    pass


class BeteraJobsSourceConfig(JobsSourceConfig):
    def __init__(self, filters: SearchFilter):
        super().__init__('Betera', filters)


class BeteraJobsSource(JobsSource[BeteraJobsSourceConfig]):
    BASE_URL = 'https://betera.huntflow.io'
    API_URL = f'{BASE_URL}/api/vacancy'
    GENERAL_APPLICATION_SLUG = 'khochu-rabotat-v-betera'

    async def fetch(self, since: datetime) -> list[Job]:
        # These vacancies have no publication dates; saved job history deduplicates them.
        return await asyncio.to_thread(self._fetch)

    def _fetch(self) -> list[Job]:
        filters = self.config.filters
        if not filters.keywords:
            return []
        jobs, seen, page = [], set(), 1
        try:
            while True:
                update_error_context(operation='fetch_listing', page=page, posting=None)
                listing = self._request_json(self.API_URL, params={'count': 20, 'page': page})
                update_error_context(operation='parse_listing')
                if (not isinstance(listing, dict) or not isinstance(listing.get('items'), list)
                        or type(listing.get('total')) is not int or listing['total'] < 0
                        or type(listing.get('page')) is not int or listing['page'] != page):
                    raise ValueError('Invalid vacancy listing')
                # Huntflow's total is the number of pages, not the number of jobs.
                if not listing['items'] and page < listing['total']:
                    raise ValueError('Empty intermediate page')
                new_ids = set()
                for item in listing['items']:
                    update_error_context(operation='parse_posting', posting=None)
                    identity, slug = self._identity(item)
                    url = f'{self.BASE_URL}/vacancy/{slug}'
                    update_error_context(posting=posting_context({'id': identity, 'url': url}))
                    if identity in seen:
                        continue
                    seen.add(identity)
                    new_ids.add(identity)
                    if slug == self.GENERAL_APPLICATION_SLUG or item.get('archived_at'):
                        continue
                    update_error_context(operation='fetch_posting')
                    record = self._request_json(f'{self.API_URL}/{slug}', skip_missing=True)
                    if record is None:
                        continue
                    update_error_context(operation='parse_posting')
                    if self._identity(record)[0] != identity:
                        raise ValueError('Mismatched vacancy')
                    if record.get('is_archived') or record.get('archived_at'):
                        continue
                    job = self._parse_job(record, url, identity)
                    text = f'{job.title}\n{job.description}'.casefold()
                    if any(word.casefold() in text for word in filters.exclude_keywords):
                        continue
                    if '*' not in filters.keywords and not any(word.casefold() in text for word in filters.keywords):
                        continue
                    jobs.append(job)
                if page >= listing['total']:
                    break
                if not new_ids:
                    raise ValueError('Repeated listing page')
                page += 1
        except (ValueError, TypeError, KeyError, OverflowError):
            raise BeteraFetchError('Betera returned invalid job data') from None
        return jobs

    @staticmethod
    def _request_json(url: str, *, params=None, skip_missing=False):
        try:
            response = requests.get(url, params=params, headers={'User-Agent': 'Mozilla/5.0'}, timeout=30)
            if skip_missing and response.status_code in (404, 410):
                return None
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError('Invalid vacancy response')
            return data
        except requests.HTTPError as error:
            status = error.response.status_code if error.response is not None else 'unknown'
            message = f'Betera returned HTTP {status}'
            if status == 429:
                message += '; rate limit exceeded, try again later'
            raise BeteraFetchError(message) from None
        except requests.exceptions.JSONDecodeError:
            raise BeteraFetchError('Betera returned invalid job data') from None
        except requests.RequestException:
            raise BeteraFetchError('Betera request failed; check network connectivity') from None

    @staticmethod
    def _identity(record) -> tuple[int, str]:
        if (not isinstance(record, dict) or type(record.get('id')) is not int or record['id'] <= 0
                or not isinstance(record.get('slug'), str)
                or not re.fullmatch(r'[a-zA-Z0-9_-]+', record['slug'])):
            raise ValueError('Invalid vacancy identity')
        return record['id'], record['slug']

    @staticmethod
    def _parse_job(record, url: str, identity: int) -> Job:
        title = record['position']
        if not isinstance(title, str) or not title.strip():
            raise ValueError('Missing title')
        parts = []
        conditions = ''
        for field, label in (('intro', ''), ('body', 'Обязанности'),
                             ('requirements', 'Требования'), ('conditions', 'Условия')):
            value = record[field]
            if value is None:
                continue
            if not isinstance(value, str):
                raise ValueError('Invalid description')
            soup = BeautifulSoup(value, 'html.parser')
            for tag in soup.select('script, style'):
                tag.decompose()
            text = soup.get_text('\n', strip=True)
            if text:
                parts.append(f'{label}\n{text}' if label else text)
            if field == 'conditions':
                conditions = text
        if not parts:
            raise ValueError('Missing description')
        parts.append('Company: Betera')
        for field, label in (('city', 'Location'), ('division', 'Department')):
            value = record.get(field)
            if value is not None:
                if not isinstance(value, str):
                    raise ValueError('Invalid metadata')
                if value.strip():
                    parts.append(f'{label}: {value.strip()}')
        salary = record.get('money')
        if salary is not None and not isinstance(salary, str):
            raise ValueError('Invalid salary')
        remote = None
        if re.search(r'гибридн\w*\s+формат|удал[её]нн\w*\s+(?:формат|работа)|\b(?:hybrid|remote) work\b',
                     conditions, re.IGNORECASE):
            remote = True
        return Job(title=title.strip(), description='\n\n'.join(parts), salary=salary or '-',
                   is_remote_allowed=remote, url=url,
                   fingerprint=hashlib.sha256(f'betera.huntflow.io:{identity}'.encode()).hexdigest())
