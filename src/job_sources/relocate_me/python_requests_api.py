import json
import math
import re
from urllib.parse import parse_qs, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from src.job_sources.relocate_me.api import (
    BASE_URL, RelocateMeApi, RelocateMeFetchError, is_expired, listing_url, validate_posting,
)


class PythonRequestsModuleRelocateMeApi(RelocateMeApi):
    HEADERS = {'User-Agent': 'Mozilla/5.0', 'Accept': 'text/html'}
    TIMEOUT_SECONDS = 30

    def get_jobs_page(self, page: int) -> dict:
        response = self._request(BASE_URL + '/international-jobs', params={'page': page})
        try:
            soup = BeautifulSoup(response.text, 'html.parser')
            count = None
            for heading in soup.find_all('h1'):
                match = re.fullmatch(r'([0-9]+)\s+jobs?\s+available', heading.get_text(' ', strip=True))
                if match:
                    count = int(match[1])
                    break
            if count is None:
                raise ValueError('Missing results heading')
            urls = []
            if count:
                container = soup.select_one('.jobs-list')
                if container is None:
                    raise ValueError('Missing job listings')
                links = container.select('.jobs-list__job .job__title > a[href]')
                if not links:
                    raise ValueError('Missing job links')
                for link in links:
                    url, _ = listing_url(link['href'])
                    # This partner sells access to an archive; it is not a vacancy.
                    # Other sponsored vacancies remain eligible.
                    if urlsplit(url).path.startswith('/remote/remote/the-global-move/'):
                        continue
                    urls.append(url)
            next_page = None
            link = soup.select_one('a[aria-label="Next page"]:not(.disabled):not([aria-disabled="true"])')
            if link is not None:
                parts = urlsplit(urljoin(BASE_URL, link['href']))
                if parts.netloc != 'relocate.me' or parts.scheme != 'https' or parts.path != '/international-jobs':
                    raise ValueError('Invalid pagination URL')
                values = parse_qs(parts.query).get('page', [])
                if len(values) != 1:
                    raise ValueError('Invalid pagination')
                next_page = int(values[0])
                if next_page != page + 1 or not urls:
                    raise ValueError('Invalid next page')
            return {'urls': list(dict.fromkeys(urls)), 'next_page': next_page}
        except (ValueError, TypeError, KeyError):
            raise RelocateMeFetchError('Relocate.me returned invalid listing data') from None

    def get_job_posting(self, url: str) -> dict | None:
        try:
            url, identity = listing_url(url)
            response = self._request(url, skip_missing=True)
            if response is None:
                return None
            soup = BeautifulSoup(response.text, 'html.parser')
            if soup.select_one('.archive-vacancy') is not None:
                return None
            schema = self._job_schema(soup)
            identifier = schema.get('identifier')
            if not isinstance(identifier, dict) or str(identifier.get('value')) != identity:
                raise ValueError('Mismatched job identity')
            if is_expired(schema.get('validThrough')):
                return None
            heading = soup.select_one('.job-info__heading h1')
            sections = soup.select('.job-info__description-item')
            if heading is None or not sections:
                raise ValueError('Missing job content')
            description = '\n\n'.join(section.get_text('\n', strip=True) for section in sections)
            company = soup.select_one('.job-info__company')
            location = soup.select_one('.job-info__country')
            relocation = soup.select_one('.job-info__relocation-packages')
            for element, label in ((company, 'Company'), (location, 'Location'), (relocation, 'Relocation')):
                if element is not None:
                    description += f'\n\n{label}: {element.get_text(" ", strip=True)}'
            tags = soup.select('.job-info a[href*="international-jobs?query="]')
            if tags:
                description += '\n\nTags: ' + ', '.join(tag.get_text(' ', strip=True) for tag in tags)
            # Only an explicit remote marker is conclusive; relocation alone is not.
            remote = True if schema.get('jobLocationType') == 'TELECOMMUTE' else None
            if location is not None and re.search(r'\bRemote\b', location.get_text(' ', strip=True), re.I):
                remote = True
            posting = {
                'id': identity,
                'title': heading.get_text(' ', strip=True),
                'description': description,
                'date_posted': schema['datePosted'],
                'salary': self._salary(schema, sections),
                'remote': remote,
                'valid_through': schema.get('validThrough'),
            }
            validate_posting(posting)
            return posting
        except (ValueError, TypeError, KeyError, OverflowError):
            raise RelocateMeFetchError('Relocate.me returned invalid job data') from None

    def _request(self, url: str, *, params: dict | None = None, skip_missing: bool = False):
        try:
            response = requests.get(url, params=params, headers=self.HEADERS, timeout=self.TIMEOUT_SECONDS)
            if skip_missing and response.status_code in (404, 410):
                return None
            response.raise_for_status()
            return response
        except requests.HTTPError as error:
            status = error.response.status_code if error.response is not None else 'unknown'
            message = f'Relocate.me returned HTTP {status}'
            if status == 429:
                message += '; rate limit exceeded, try again later'
            raise RelocateMeFetchError(message) from None
        except requests.RequestException:
            raise RelocateMeFetchError('Relocate.me request failed; check network connectivity') from None

    def _job_schema(self, soup: BeautifulSoup) -> dict:
        for script in soup.find_all('script', type='application/ld+json'):
            try:
                # Live pages contain literal tabs/newlines inside JSON strings.
                payload = json.loads(script.get_text(), strict=False)
            except ValueError:
                continue
            candidates = payload if isinstance(payload, list) else [payload]
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue
                if isinstance(candidate.get('@graph'), list):
                    candidates.extend(candidate['@graph'])
                kind = candidate.get('@type')
                if kind == 'JobPosting' or isinstance(kind, list) and 'JobPosting' in kind:
                    return candidate
        raise ValueError('Missing JobPosting metadata')

    def _salary(self, schema: dict, sections: list) -> str:
        salary = schema.get('baseSalary')
        if salary:
            if not isinstance(salary, dict) or not isinstance(salary.get('value'), dict):
                raise ValueError('Invalid structured salary')
            value = salary['value']
            minimum, maximum = value.get('minValue', value.get('value')), value.get('maxValue', value.get('value'))
            for amount in (minimum, maximum):
                if amount is not None and (type(amount) not in (int, float) or not math.isfinite(amount) or amount < 0):
                    raise ValueError('Invalid salary amount')
            currency, period = salary.get('currency'), value.get('unitText')
            if any(item is not None and not isinstance(item, str) for item in (currency, period)):
                raise ValueError('Invalid salary units')
            if minimum is not None or maximum is not None:
                text = (f'up to {maximum:,.15g}' if minimum is None else f'from {minimum:,.15g}' if maximum is None
                        else f'{minimum:,.15g}' if minimum == maximum else f'{minimum:,.15g} - {maximum:,.15g}')
                return (f'{currency} ' if currency else '') + text + (f' / {period.lower()}' if period else '')
        for section in sections:
            # Preserve explicit salary text without interpreting benefits as pay.
            for block in section.find_all(['p', 'li']):
                match = re.search(r'\b(?:Annual |Monthly |Base )?salary(?: range)?\s*:\s*[^\n]+',
                                  block.get_text(' ', strip=True), re.I)
                if match:
                    return match[0]
        return '-'
