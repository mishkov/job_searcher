# Project setup

before running the script copy `config.py` and edit as you need. See `config.example.py` for examples.

```shell
cp config.example.py config.py
```

The Web3.career source uses the [official free API](https://web3.career/web3-jobs-api)
to fetch Flutter vacancies. Obtain a token there and add it to `.env`:

```dotenv
WEB3_CAREER_API_TOKEN=your_token
```

Add `Web3CareerJobsSourceConfig(filters)` to `config.py`'s `SOURCES`, importing it
from `src.job_sources.web3_career.web3_career`. The source reads the token when
searching, respects the search date and keyword filters, and includes full job
descriptions. It requests the latest 100 Flutter jobs; the API has no pagination,
so older history beyond those results is unavailable. Access is rate limited.
Salary amounts retain currency and period when supplied; estimates are labeled
and their currency/period are not inferred when absent.

Application buttons preserve the API's `apply_url` exactly, as required by its
attribution terms. Keep the token private. The registration email restricts the
token to your website; confirm with Web3.career whether your Telegram bot is
covered by that usage scope. The source no longer needs Playwright or a browser.
API failures are reported without stopping searches of the other sources.

NoDesk is included in `config.example.py` and requires no account or private API
token. For an existing `config.py`, import `NoDeskJobsSourceConfig` from
`src.job_sources.nodesk.nodesk` and add `NoDeskJobsSourceConfig(_common_filters)`
to `SOURCES` (or supply your own `SearchFilter`). It uses the public Algolia search
configured by NoDesk's frontend, follows all result pages with an empty query,
and filters locally so keywords found only in full descriptions are included.
The public search credentials may change if NoDesk updates its frontend.

New NoDesk-hosted vacancies are fetched one at a time and their `JobPosting`
structured data supplies the full description and salary currency/period when
available. The publication cutoff is inclusive; timestamps without an offset
are treated as UTC. Advertised vacancies remain eligible, including records
marked `is_ad=true`. External listings retain their provided application URL,
advertising description and badges; external sites are not crawled, so those
descriptions may be shorter. Duplicate identities are fetched once per search,
removed detail pages are skipped, and source failures do not stop other sources.
Use of this source remains subject to [NoDesk's terms](https://nodesk.co/legal/terms/).

Welcome to the Jungle requires no API token and is included in
`config.example.py`. To enable it in an existing `config.py`, import
`WelcomeToTheJungleJobsSourceConfig` from
`src.job_sources.welcome_to_the_jungle.welcome_to_the_jungle` and add
`WelcomeToTheJungleJobsSourceConfig(SearchFilter(keywords=['flutter']))` to
`SOURCES`.

Run the API source tests without Telegram or database access:

```shell
python -m unittest discover -s tests
```
