import re
from xml.etree import ElementTree

import requests

from src.error_reporting import posting_context, update_error_context
from src.job_sources.we_work_remotely.api import (
    CATEGORIES, FIELDS, FeedResponse, WeWorkRemotelyApi, WeWorkRemotelyFetchError, validate_jobs,
)


class PythonRequestsModuleWeWorkRemotelyApi(WeWorkRemotelyApi):
    BASE_URL = 'https://weworkremotely.com/categories/'
    HEADERS = {'User-Agent': 'Mozilla/5.0', 'Accept': 'application/rss+xml'}
    TIMEOUT_SECONDS = 30

    def get_feed(self, category: str, *, etag: str | None = None,
                 last_modified: str | None = None) -> FeedResponse:
        if category not in CATEGORIES:
            raise ValueError('Unknown WWR category')
        headers = dict(self.HEADERS)
        if etag:
            headers['If-None-Match'] = etag
        if last_modified:
            headers['If-Modified-Since'] = last_modified
        try:
            response = requests.get(
                f'{self.BASE_URL}{category}.rss', headers=headers, timeout=self.TIMEOUT_SECONDS,
            )
            response.raise_for_status()
        except requests.HTTPError as error:
            status = error.response.status_code if error.response is not None else 'unknown'
            message = f'We Work Remotely returned HTTP {status}'
            if status == 429:
                message += '; rate limit exceeded, try again later'
            raise WeWorkRemotelyFetchError(message) from None
        except requests.RequestException:
            raise WeWorkRemotelyFetchError(
                'We Work Remotely request failed; check network connectivity',
            ) from None

        validators = {
            'etag': response.headers.get('ETag'),
            'last_modified': response.headers.get('Last-Modified'),
        }
        if response.status_code == 304:
            if not (etag or last_modified):
                raise WeWorkRemotelyFetchError('We Work Remotely returned an unexpected HTTP 304')
            return FeedResponse(None, **validators)
        try:
            # RSS does not need DTDs; reject entity declarations before parsing.
            if re.search(br'<!\s*(DOCTYPE|ENTITY)\b', response.content, re.IGNORECASE):
                raise ValueError('Unsupported XML declaration')
            root = ElementTree.fromstring(response.content)
            channel = root.find('channel')
            if root.tag != 'rss' or channel is None:
                raise ValueError('Expected RSS channel')
            jobs = [{field: item.findtext(field) or '' for field in FIELDS}
                    for item in channel.findall('item')]
            for job in jobs:
                update_error_context(operation='parse_posting', posting=posting_context(job))
                validate_jobs([job])
            return FeedResponse(jobs, **validators)
        except (ElementTree.ParseError, ValueError, TypeError, IndexError, OverflowError):
            raise WeWorkRemotelyFetchError('We Work Remotely returned invalid RSS job data') from None
