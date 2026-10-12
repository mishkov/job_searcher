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

The Welcome to the Jungle source uses its public jobs API without a token. Add
`WelcomeToTheJungleJobsSourceConfig(filters)` to `config.py`'s `SOURCES`, importing
it from `src.job_sources.welcome_to_the_jungle.welcome_to_the_jungle` (also shown
in `config.example.py`). It searches each keyword and follows all pages with a
fresh random `query_id` per keyword search. A wildcard searches without a title
filter. Results are filtered by `published_at`, regardless of relevance order,
and deduplicated by the provider's job reference across pages and keywords.

For each recent, published vacancy, the source fetches full details from
`/api/v1/organizations/{organization_slug}/jobs/{job_slug}`. Messages include
the job description, preferred experience and recruitment process, followed by
the company summary, location, contract and remote format. Keyword and exclusion
filters apply to the title and this full text. Duplicate references are fetched
only once per search, including jobs rejected by the filters. Removed vacancies
(HTTP 404/410 or an unpublished detail status) are skipped. Empty description
fields fall back to the listing summary; other detail API failures are reported.
Both endpoints work with ordinary Python `requests`, without browser access,
cookies or authentication. The bot displays the first 500 description characters.
Hybrid and fully remote jobs allow remote work; unknown remote status stays
unknown. Salaries retain the API's amounts, currency and period without inferring
units. API failures are reported without stopping the other sources.

Run the API source tests without Telegram or database access:

```shell
python -m unittest discover -s tests
```
