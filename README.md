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

Run the Web3.career tests without Telegram or database access:

```shell
python -m unittest discover -s tests
```
