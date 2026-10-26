import asyncio
import hashlib
import re
from datetime import datetime
from urllib.parse import parse_qs, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from src.error_reporting import posting_context, update_error_context
from src.job import Job
from src.job_sources.job_source import JobsSource, JobsSourceConfig
from src.search_filter import SearchFilter


class AjBellFetchError(RuntimeError):
    pass


class AjBellJobsSourceConfig(JobsSourceConfig):
    def __init__(self, filters: SearchFilter):
        super().__init__('AJ Bell techHub', filters)


class AjBellJobsSource(JobsSource[AjBellJobsSourceConfig]):
    LISTING_URL = 'https://www.ajbell.co.uk/group/techhub/vacancies'

    async def fetch(self, since: datetime) -> list[Job]:
        # AJ Bell publishes no dates. Keep active jobs eligible on every run;
        # persisted seen-job history prevents repeat deliveries.
        return await asyncio.to_thread(self._fetch)

    def _fetch(self) -> list[Job]:
        filters = self.config.filters
        if not filters.keywords:
            return []
        jobs, seen, pages = [], set(), set()
        page_url = self.LISTING_URL
        try:
            while page_url:
                update_error_context(operation='fetch_listing', page=len(pages), posting=None)
                if page_url in pages:
                    raise ValueError('Repeated listing page')
                pages.add(page_url)
                html = self._request_html(page_url)
                update_error_context(operation='parse_listing')
                soup = BeautifulSoup(html, 'html.parser')
                listing = soup.select_one('.block-views-blockcareers-techhub')
                if listing is None or listing.select_one('#views-uikit-careers-techhub, .view-empty, .views-empty') is None:
                    raise ValueError('Missing vacancy listing')
                for card in listing.select('.card-career'):
                    update_error_context(operation='parse_posting', posting=None)
                    link = card.find_parent('a')
                    url, identity = self._job_url(link.get('href') if link else None)
                    update_error_context(posting=posting_context({'id': identity, 'url': url}))
                    if identity in seen:
                        continue
                    seen.add(identity)
                    update_error_context(operation='fetch_posting')
                    detail = self._request_html(url, skip_missing=True)
                    if detail is None:
                        continue
                    update_error_context(operation='parse_posting')
                    job = self._parse_job(BeautifulSoup(detail, 'html.parser'), url, identity)
                    text = f'{job.title}\n{job.description}'.casefold()
                    if any(word.casefold() in text for word in filters.exclude_keywords):
                        continue
                    if '*' not in filters.keywords and not any(word.casefold() in text for word in filters.keywords):
                        continue
                    jobs.append(job)
                update_error_context(operation='parse_listing', posting=None)
                next_link = listing.select_one('.pager__item--next a, a[rel="next"]')
                page_url = self._next_page_url(next_link.get('href')) if next_link else None
        except (ValueError, TypeError, KeyError, OverflowError):
            raise AjBellFetchError('AJ Bell returned invalid job data') from None
        return jobs

    @staticmethod
    def _request_html(url: str, *, skip_missing: bool = False):
        try:
            response = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=30)
            if skip_missing and response.status_code in (404, 410):
                return None
            response.raise_for_status()
            return response.text
        except requests.HTTPError as error:
            status = error.response.status_code if error.response is not None else 'unknown'
            message = f'AJ Bell returned HTTP {status}'
            if status == 429:
                message += '; rate limit exceeded, try again later'
            raise AjBellFetchError(message) from None
        except requests.RequestException:
            raise AjBellFetchError('AJ Bell request failed; check network connectivity') from None

    @classmethod
    def _job_url(cls, value) -> tuple[str, str]:
        if not isinstance(value, str):
            raise ValueError('Missing job URL')
        parts = urlsplit(urljoin(cls.LISTING_URL, value))
        identity = re.fullmatch(r'/group/techhub/vacancies/([0-9]+)/?', parts.path)
        if parts.scheme != 'https' or parts.netloc != 'www.ajbell.co.uk' or not identity:
            raise ValueError('Invalid job URL')
        return f'{cls.LISTING_URL}/{identity[1]}', identity[1]

    @classmethod
    def _next_page_url(cls, value) -> str:
        if not isinstance(value, str):
            raise ValueError('Missing next page URL')
        parts = urlsplit(urljoin(cls.LISTING_URL, value))
        query = parse_qs(parts.query, keep_blank_values=True)
        if (parts.scheme != 'https' or parts.netloc != 'www.ajbell.co.uk'
                or parts.path.rstrip('/') != urlsplit(cls.LISTING_URL).path
                or set(query) != {'page'} or len(query['page']) != 1 or not query['page'][0].isascii()
                or not query['page'][0].isdigit()):
            raise ValueError('Invalid next page URL')
        page = int(query['page'][0])
        return f'{cls.LISTING_URL}?page={page}' if page else cls.LISTING_URL

    @staticmethod
    def _parse_job(posting: BeautifulSoup, url: str, identity: str) -> Job:
        title_tag = posting.select_one('main h1')
        description_tag = posting.select_one('.block-views-blockvacancy-vacancy .field-content')
        if title_tag is None or description_tag is None:
            raise ValueError('Missing title or description')
        for tag in description_tag.select('script, style'):
            tag.decompose()
        title = title_tag.get_text(' ', strip=True)
        description = description_tag.get_text('\n', strip=True)
        if not title or not description:
            raise ValueError('Empty title or description')
        parts = [description, 'Company: AJ Bell']
        location = ''
        for icon, label in (('location-pin', 'Location'), ('briefcase', 'Department')):
            marker = posting.select_one(f'.block-views-blockvacancy-meta .item-icon [uk-icon="{icon}"]')
            if marker is not None:
                value = marker.parent.get_text(' ', strip=True)
                if value:
                    parts.append(f'{label}: {value}')
                    if label == 'Location':
                        location = value
        remote = True if re.search(r'\b(remote|hybrid)\b', location, re.IGNORECASE) else None
        return Job(title=title, description='\n\n'.join(parts), salary='-', is_remote_allowed=remote, url=url,
                   fingerprint=hashlib.sha256(f'ajbell.co.uk:{identity}'.encode()).hexdigest())
