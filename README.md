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

NoDesk requires no API token and is included in `config.example.py`. To enable it
in an existing `config.py`, import `NoDeskJobsSourceConfig` from
`src.job_sources.nodesk.nodesk` and add
`NoDeskJobsSourceConfig(SearchFilter(keywords=['flutter']))` to `SOURCES`.

Welcome to the Jungle requires no API token and is included in
`config.example.py`. To enable it in an existing `config.py`, import
`WelcomeToTheJungleJobsSourceConfig` from
`src.job_sources.welcome_to_the_jungle.welcome_to_the_jungle` and add
`WelcomeToTheJungleJobsSourceConfig(SearchFilter(keywords=['flutter']))` to
`SOURCES`.

Andersen's [vacancies](https://people-andersenlab.com/vacancies) require no API
token and are included in `config.example.py`. To enable this source in an
existing `config.py`, import `AndersenJobsSourceConfig` from
`src.job_sources.andersen.andersen` and add
`AndersenJobsSourceConfig(SearchFilter(keywords=['flutter']))` to `SOURCES`.
Each search fetches the complete active list from the public v5 API, including
descriptions, requirements, responsibilities, benefits, and hiring locations.
Keywords and exclusions match the title and full description. The API currently
omits publication dates, so all matching active jobs are eligible regardless of
the search cutoff; the existing seen-job history prevents repeat notifications.
If an opening date is supplied, the source applies the cutoff. Salary amounts
retain the supplied currency without assuming a pay period, and remote
availability follows the API's `isRemote` flag. Source failures are reported
without stopping searches of other sources.

Run the API source tests without Telegram or database access:

```shell
python -m unittest discover -s tests
```
