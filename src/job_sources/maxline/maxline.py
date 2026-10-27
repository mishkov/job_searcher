import asyncio
import hashlib
import re
from datetime import datetime
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from src.error_reporting import posting_context, update_error_context
from src.job import Job
from src.job_sources.job_source import JobsSource, JobsSourceConfig
from src.search_filter import SearchFilter


class MaxLineFetchError(RuntimeError):
    pass


class MaxLineJobsSourceConfig(JobsSourceConfig):
    def __init__(self, filters: SearchFilter):
        super().__init__('MaxLine', filters)


class MaxLineJobsSource(JobsSource[MaxLineJobsSourceConfig]):
    LISTING_URL = 'https://hr.maxline.by/'

    async def fetch(self, since: datetime) -> list[Job]:
        # These vacancies have no publication dates; saved job history deduplicates them.
        return await asyncio.to_thread(self._fetch)

    def _fetch(self) -> list[Job]:
        filters = self.config.filters
        if not filters.keywords:
            return []
        jobs, seen = [], set()
        try:
            update_error_context(operation='fetch_listing', page=1, posting=None)
            soup = BeautifulSoup(self._request_html(self.LISTING_URL), 'html.parser')
            update_error_context(operation='parse_listing')
            listing = soup.select_one('#vacanciesCarousel')
            if listing is None:
                raise ValueError('Missing vacancy listing')
            for link in listing.select('a.vacancy-card'):
                update_error_context(operation='parse_posting', posting=None)
                url, identity = self._job_url(link.get('href'))
                update_error_context(posting=posting_context({'id': identity, 'url': url}))
                if identity in seen:
                    continue
                seen.add(identity)
                update_error_context(operation='fetch_posting')
                html = self._request_html(url, skip_missing=True)
                if html is None:
                    continue
                update_error_context(operation='parse_posting')
                job = self._parse_job(BeautifulSoup(html, 'html.parser'), url, identity)
                text = f'{job.title}\n{job.description}'.casefold()
                if any(word.casefold() in text for word in filters.exclude_keywords):
                    continue
                if '*' not in filters.keywords and not any(word.casefold() in text for word in filters.keywords):
                    continue
                jobs.append(job)
        except (ValueError, TypeError, KeyError, OverflowError):
            raise MaxLineFetchError('MaxLine returned invalid job data') from None
        return jobs

    @staticmethod
    def _request_html(url: str, *, skip_missing=False):
        try:
            response = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=30)
            if skip_missing and response.status_code in (404, 410):
                return None
            response.raise_for_status()
            return response.text
        except requests.HTTPError as error:
            status = error.response.status_code if error.response is not None else 'unknown'
            message = f'MaxLine returned HTTP {status}'
            if status == 429:
                message += '; rate limit exceeded, try again later'
            raise MaxLineFetchError(message) from None
        except requests.RequestException:
            raise MaxLineFetchError('MaxLine request failed; check network connectivity') from None

    @classmethod
    def _job_url(cls, value) -> tuple[str, str]:
        if not isinstance(value, str):
            raise ValueError('Missing job URL')
        parts = urlsplit(urljoin(cls.LISTING_URL, value))
        match = re.fullmatch(r'/vacancies/([a-zA-Z0-9_-]+)/?', parts.path)
        if parts.scheme != 'https' or parts.netloc != 'hr.maxline.by' or not match:
            raise ValueError('Invalid job URL')
        return f'{cls.LISTING_URL}vacancies/{match[1]}/', match[1]

    @staticmethod
    def _parse_job(soup: BeautifulSoup, url: str, identity: str) -> Job:
        posting = soup.select_one('.vacancy-page__container')
        title = posting.select_one('.vacancy-page__title') if posting else None
        blocks = posting.select('.vacancy-page__block') if posting else []
        if title is None or not title.get_text(strip=True) or not blocks:
            raise ValueError('Missing title or description')
        parts = []
        for block in posting.select('.vacancy-page__summary, .vacancy-page__block'):
            for tag in block.select('script, style'):
                tag.decompose()
            text = block.get_text('\n', strip=True)
            if text:
                parts.append(text)
        if not any(li.get_text(strip=True) for block in blocks for li in block.select('.vacancy-page__list li')):
            raise ValueError('Missing job requirements')
        # The careers site specifies office work in Minsk for these vacancies.
        parts.extend(('Company: MaxLine', 'Location: Минск', 'Workplace: Office'))
        return Job(title=title.get_text(' ', strip=True), description='\n\n'.join(parts), salary='-',
                   is_remote_allowed=False, url=url,
                   fingerprint=hashlib.sha256(f'hr.maxline.by:{identity}'.encode()).hexdigest())
