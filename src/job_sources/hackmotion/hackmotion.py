import asyncio
import hashlib
import json
import re
from datetime import datetime, timezone
from html import unescape
from urllib.parse import parse_qs, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from src.error_reporting import posting_context, update_error_context
from src.job import Job
from src.job_sources.job_source import JobsSource, JobsSourceConfig
from src.search_filter import SearchFilter


class HackMotionFetchError(RuntimeError):
    pass


class HackMotionJobsSourceConfig(JobsSourceConfig):
    def __init__(self, filters: SearchFilter):
        super().__init__('HackMotion', filters)


class HackMotionJobsSource(JobsSource[HackMotionJobsSourceConfig]):
    LISTING_URL = 'https://jobs.hackmotion.com/jobs'

    async def fetch(self, since: datetime) -> list[Job]:
        return await asyncio.to_thread(self._fetch, since)

    def _fetch(self, since: datetime) -> list[Job]:
        filters = self.config.filters
        if not filters.keywords:
            return []
        cutoff = since.replace(tzinfo=timezone.utc) if since.tzinfo is None else since.astimezone(timezone.utc)
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
                listing = soup.select_one('#jobs_list')
                if listing is None or listing.select_one('#jobs_list_container') is None:
                    raise ValueError('Missing vacancy listing')
                for link in listing.select('#jobs_list_container a[href]'):
                    update_error_context(operation='parse_posting', posting=None)
                    url, identity = self._job_url(link['href'])
                    update_error_context(posting=posting_context({'id': identity, 'url': url}))
                    if identity in seen:
                        continue
                    seen.add(identity)
                    update_error_context(operation='fetch_posting')
                    detail = self._request_html(url, skip_missing=True)
                    if detail is None:
                        continue
                    update_error_context(operation='parse_posting')
                    job, posted = self._parse_job(BeautifulSoup(detail, 'html.parser'), url, identity)
                    posted_at = datetime.fromisoformat(posted)
                    if posted_at.tzinfo is None:
                        posted_at = posted_at.replace(tzinfo=timezone.utc)
                    posted_at = posted_at.astimezone(timezone.utc)
                    if (posted_at.date() < cutoff.date() if len(posted) == 10 else posted_at < cutoff):
                        continue
                    text = f'{job.title}\n{job.description}'.casefold()
                    if any(word.casefold() in text for word in filters.exclude_keywords):
                        continue
                    if '*' not in filters.keywords and not any(word.casefold() in text for word in filters.keywords):
                        continue
                    jobs.append(job)
                update_error_context(operation='parse_listing', posting=None)
                next_link = listing.select_one('a[rel="next"], .pagination a.next')
                page_url = self._next_page_url(next_link.get('href')) if next_link else None
        except (ValueError, TypeError, KeyError, OverflowError):
            raise HackMotionFetchError('HackMotion returned invalid job data') from None
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
            message = f'HackMotion returned HTTP {status}'
            if status == 429:
                message += '; rate limit exceeded, try again later'
            raise HackMotionFetchError(message) from None
        except requests.RequestException:
            raise HackMotionFetchError('HackMotion request failed; check network connectivity') from None

    @classmethod
    def _job_url(cls, value) -> tuple[str, str]:
        if not isinstance(value, str):
            raise ValueError('Missing job URL')
        parts = urlsplit(urljoin(cls.LISTING_URL, value))
        match = re.fullmatch(r'/jobs/([0-9]+)-[^/]+/?', parts.path)
        if parts.scheme != 'https' or parts.netloc != 'jobs.hackmotion.com' or not match:
            raise ValueError('Invalid job URL')
        return f'https://jobs.hackmotion.com{parts.path.rstrip("/")}', match[1]

    @classmethod
    def _next_page_url(cls, value) -> str:
        if not isinstance(value, str):
            raise ValueError('Missing next page URL')
        parts = urlsplit(urljoin(cls.LISTING_URL, value))
        query = parse_qs(parts.query, keep_blank_values=True)
        if (parts.scheme != 'https' or parts.netloc != 'jobs.hackmotion.com' or parts.path.rstrip('/') != '/jobs'
                or set(query) != {'page'} or len(query['page']) != 1 or not query['page'][0].isascii()
                or not query['page'][0].isdigit()):
            raise ValueError('Invalid next page URL')
        page = int(query['page'][0])
        return f'{cls.LISTING_URL}?page={page}' if page > 1 else cls.LISTING_URL

    @staticmethod
    def _parse_job(soup: BeautifulSoup, url: str, identity: str) -> tuple[Job, str]:
        records = []
        for script in soup.select('script[type="application/ld+json"]'):
            data = json.loads(script.get_text())
            records.extend(data if isinstance(data, list) else [data])
        postings = [record for record in records if isinstance(record, dict) and record.get('@type') == 'JobPosting']
        if len(postings) != 1:
            raise ValueError('Missing job posting')
        record = postings[0]
        title, description, posted = record['title'], record['description'], record['datePosted']
        if not all(isinstance(value, str) and value.strip() for value in (title, description, posted)):
            raise ValueError('Missing title, description or date')
        body = BeautifulSoup(unescape(description), 'html.parser')
        for tag in body.select('script, style'):
            tag.decompose()
        description = body.get_text('\n', strip=True)
        if not description:
            raise ValueError('Empty description')
        parts = [description, 'Company: HackMotion']
        remote = True if record.get('jobLocationType') == 'TELECOMMUTE' else None
        for term in soup.select('dl dt'):
            label = term.get_text(' ', strip=True)
            if label not in ('Location', 'Locations', 'Department', 'Remote status'):
                continue
            definition = term.find_next_sibling('dd')
            value = definition.get_text(' ', strip=True) if definition else ''
            if value:
                parts.append(f'{label}: {value}')
                if label == 'Remote status':
                    remote = {'hybrid': True, 'fully remote': True, 'temporarily remote': True,
                              'no remote work': False}.get(value.casefold(), remote)
        salary_line = re.search(r'^Salary\s+(.+)$', description, re.IGNORECASE | re.MULTILINE)
        job = Job(title=title.strip(), description='\n\n'.join(parts),
                  salary=salary_line[1] if salary_line else '-', is_remote_allowed=remote, url=url,
                  fingerprint=hashlib.sha256(f'jobs.hackmotion.com:{identity}'.encode()).hexdigest())
        return job, posted
