import json
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

from src.job_sources.nodesk.api import NoDeskApi, NoDeskFetchError


class PythonRequestsModuleNoDeskApi(NoDeskApi):
    API_URL = 'https://0586l1sok8-dsn.algolia.net/1/indexes/*/queries'
    # Public search credentials shipped in NoDesk's frontend, not an account token.
    API_PARAMS = {
        'x-algolia-application-id': '0586L1SOK8',
        'x-algolia-api-key': '8dacb58c6f375cba28e19ecf1f03e9e1',
    }
    HEADERS = {
        'User-Agent': 'PostmanRuntime/7.43.0',
        'Origin': 'https://nodesk.co',
        'Referer': 'https://nodesk.co/',
    }
    TIMEOUT_SECONDS = 30

    def get_jobs_page(self, page: int, page_size: int) -> dict:
        response = self._request(self.API_URL, body={'requests': [{
            'indexName': 'jobPosts',
            'params': urlencode({
                'filters': 'searchFilter:remote-jobs',
                'hitsPerPage': page_size,
                # Title/tag queries would lose description-only matches.
                'query': '',
                'page': page,
                'attributesToHighlight': '[]',
            }),
        }]})
        try:
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError('Expected search results')
            return payload
        except ValueError:
            raise NoDeskFetchError('NoDesk returned invalid job data') from None

    def get_job_posting(self, url: str) -> dict | None:
        response = self._request(url, skip_missing=True)
        if response is None:
            return None
        try:
            return self._job_posting(response.text)
        except (ValueError, TypeError):
            raise NoDeskFetchError('NoDesk returned invalid job data') from None

    def _request(self, url: str, *, body: dict | None = None, skip_missing: bool = False):
        try:
            if body is None:
                response = requests.get(url, headers=self.HEADERS, timeout=self.TIMEOUT_SECONDS)
            else:
                response = requests.post(
                    url, params=self.API_PARAMS, json=body, headers=self.HEADERS,
                    timeout=self.TIMEOUT_SECONDS,
                )
            if skip_missing and response.status_code in (404, 410):
                return None
            response.raise_for_status()
            return response
        except requests.HTTPError as error:
            status = error.response.status_code if error.response is not None else 'unknown'
            message = f'NoDesk returned HTTP {status}'
            if status == 429:
                message += '; rate limit exceeded, try again later'
            raise NoDeskFetchError(message) from None
        except requests.RequestException:
            raise NoDeskFetchError('NoDesk request failed; check network connectivity') from None

    def _job_posting(self, html: str) -> dict:
        soup = BeautifulSoup(html, 'html.parser')
        for script in soup.find_all('script', type='application/ld+json'):
            try:
                payload = json.loads(script.get_text())
            except ValueError:
                continue
            candidates = payload if isinstance(payload, list) else [payload]
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue
                graph = candidate.get('@graph')
                if isinstance(graph, list):
                    candidates.extend(graph)
                kind = candidate.get('@type')
                if kind == 'JobPosting' or isinstance(kind, list) and 'JobPosting' in kind:
                    description = candidate.get('description')
                    if not isinstance(description, str) or not description.strip():
                        raise ValueError('Missing job description')
                    return candidate
        raise ValueError('Missing JobPosting structured data')
