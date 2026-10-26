import asyncio
import hashlib
import re
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from src.error_reporting import posting_context, update_error_context
from src.job import Job
from src.job_sources.job_source import JobsSource, JobsSourceConfig
from src.search_filter import SearchFilter


class BrightstarLotteryFetchError(RuntimeError):
    pass


class BrightstarLotteryJobsSourceConfig(JobsSourceConfig):
    def __init__(self, filters: SearchFilter):
        super().__init__('Brightstar Lottery', filters)


class BrightstarLotteryJobsSource(JobsSource[BrightstarLotteryJobsSourceConfig]):
    BASE_URL = 'https://careers.brightstarlottery.com'

    async def fetch(self, since: datetime) -> list[Job]:
        return await asyncio.to_thread(self._fetch, since)

    def _fetch(self, since: datetime) -> list[Job]:
        filters = self.config.filters
        if not filters.keywords:
            return []
        cutoff = since.replace(tzinfo=timezone.utc) if since.tzinfo is None else since.astimezone(timezone.utc)
        params = {'q': '', 'locale': 'en_US', 'sortColumn': 'referencedate', 'sortDirection': 'desc'}
        update_error_context(operation='fetch_listing', page=0, posting=None)
        html = self._request_html(f'{self.BASE_URL}/search/', params=params)
        jobs = []
        seen = set()
        try:
            # Use the same pagination settings as the site's More Search Results
            # button. aria-rowcount is the number remaining, not the total.
            total = int(re.search(r'jobRecordsFound:\s*parseInt\("(\d+)"\)', html)[1])
            page_size = int(re.search(r'jobRecordsPerPage:\s*parseInt\("(\d+)"\)', html)[1])
            if page_size <= 0:
                raise ValueError('Invalid page size')
            offset = 0
            while True:
                update_error_context(operation='parse_listing', posting=None)
                soup = BeautifulSoup(html, 'html.parser')
                records = soup.select('li.job-tile')
                if len(records) != min(page_size, max(total - offset, 0)):
                    raise ValueError('Incomplete listing page')
                seen_count = len(seen)
                for record in records:
                    update_error_context(operation='parse_posting', posting=None)
                    url, identity = self._job_url(record.get('data-url'))
                    update_error_context(posting=posting_context({'id': identity, 'url': url}))
                    if identity in seen:
                        continue
                    seen.add(identity)
                    update_error_context(operation='fetch_posting')
                    detail = self._request_html(url, skip_missing=True)
                    if detail is None:
                        continue
                    update_error_context(operation='parse_posting')
                    posting = BeautifulSoup(detail, 'html.parser')
                    date_tag = posting.select_one('meta[itemprop="datePosted"]')
                    if date_tag is None:
                        closed = posting.select_one('.jobDisplayShell .job strong')
                        if closed is not None and closed.get_text(' ', strip=True) == 'Sorry, this position has been filled.':
                            continue
                        raise ValueError('Missing publication date')
                    posted_at = datetime.strptime(date_tag['content'], '%a %b %d %H:%M:%S UTC %Y').replace(
                        tzinfo=timezone.utc)
                    if posted_at < cutoff:
                        continue
                    job = self._parse_job(posting, url, identity)
                    text = f'{job.title}\n{job.description}'.casefold()
                    if any(word.casefold() in text for word in filters.exclude_keywords):
                        continue
                    if '*' not in filters.keywords and not any(word.casefold() in text for word in filters.keywords):
                        continue
                    jobs.append(job)
                if records and len(seen) == seen_count:
                    raise ValueError('Repeated listing page')
                offset += page_size
                if offset >= total:
                    break
                update_error_context(operation='fetch_listing', page=offset // page_size, posting=None)
                html = self._request_html(f'{self.BASE_URL}/tile-search-results/',
                                          params={**params, 'startrow': offset})
        except (ValueError, TypeError, KeyError, OverflowError):
            raise BrightstarLotteryFetchError('Brightstar Lottery returned invalid job data') from None
        return jobs

    def _request_html(self, url: str, *, params: dict | None = None, skip_missing: bool = False):
        try:
            response = requests.get(url, params=params, timeout=30)
            if skip_missing and response.status_code in (404, 410):
                return None
            response.raise_for_status()
            return response.text
        except requests.HTTPError as error:
            status = error.response.status_code if error.response is not None else 'unknown'
            message = f'Brightstar Lottery returned HTTP {status}'
            if status == 429:
                message += '; rate limit exceeded, try again later'
            raise BrightstarLotteryFetchError(message) from None
        except requests.RequestException:
            raise BrightstarLotteryFetchError('Brightstar Lottery request failed; check network connectivity') from None

    @classmethod
    def _job_url(cls, value) -> tuple[str, str]:
        if not isinstance(value, str):
            raise ValueError('Missing job URL')
        parts = urlsplit(urljoin(cls.BASE_URL, value))
        identity = re.fullmatch(r'/job/[^/]+/([0-9]+)/', parts.path)
        if parts.scheme != 'https' or parts.netloc != 'careers.brightstarlottery.com' or not identity:
            raise ValueError('Invalid job URL')
        return urlunsplit((parts.scheme, parts.netloc, parts.path, '', '')), identity[1]

    @staticmethod
    def _parse_job(posting: BeautifulSoup, url: str, identity: str) -> Job:
        title_tag = posting.select_one('[itemprop="title"]')
        description_tag = posting.select_one('[itemprop="description"]')
        if title_tag is None or description_tag is None:
            raise ValueError('Missing title or description')
        for tag in description_tag.select('script, style'):
            tag.decompose()
        title = title_tag.get_text(' ', strip=True)
        description = description_tag.get_text('\n', strip=True)
        if not title or not description:
            raise ValueError('Empty title or description')
        parts = [description, 'Company: Brightstar Lottery']
        location = posting.select_one('#job-location')
        if location is not None and location.get_text(' ', strip=True):
            parts.append(f"Location: {location.get_text(' ', strip=True)}")
        # Pay and workplace policies, when supplied, remain in the description.
        return Job(title=title, description='\n\n'.join(parts), salary='-', is_remote_allowed=None, url=url,
                   fingerprint=hashlib.sha256(f'careers.brightstarlottery.com:{identity}'.encode()).hexdigest())
