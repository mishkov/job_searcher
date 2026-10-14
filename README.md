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

Run the API source tests without Telegram or database access:

```shell
python -m unittest discover -s tests
```

Search history is tracked separately for each provider and filter configuration,
including the channel and topic for Telegram sources. A checkpoint advances to
the source's fetch start time only after fetching, delivering unseen jobs, and
recording their delivery all succeed. Failed sources retry their previous
interval; successful sources continue independently. Successful empty searches
also advance their checkpoint.

On startup, the bot creates the new `source_search_history` table. The old global
`last_search_time` table is left untouched and its timestamp is not reused, since
it may have advanced before a failed search. Each new source configuration starts
with the existing seven-day lookback; existing `seen_jobs` records are preserved.
Database timestamps are stored as UTC. Search history is authoritative database
state and must remain outside `CACHE_PATH`.

Overlapping `/search` commands are serialized within one bot process. Run one bot
process against this history database. Debug mode (`IS_DEBUG_MODE=true`, `1`,
`yes`, or `on`, case insensitive) uses the seven-day lookback and does not read or
write search history or seen-job records. Other values disable debug mode.
