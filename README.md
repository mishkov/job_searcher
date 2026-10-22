# Job Searcher

A Python Telegram bot for checking configured job sources and sharing matching
vacancies.

## Setup

Copy the example configuration and edit `config.py` to choose sources and search
filters:

```shell
cp config.example.py config.py
```

Set `TELEGRAM_OWNER_ID` in `.env` to your numeric Telegram user ID. The bot accepts
commands only from this user's private chat. Send `/start` to enable automatic
checks. See [bot usage](docs/BOT_USAGE.md) for commands and sleeping time.

To use Web3.career, obtain a token from its
[official API page](https://web3.career/web3-jobs-api) and add it to `.env`:

```dotenv
WEB3_CAREER_API_TOKEN=your_token
```

Add `Web3CareerJobsSourceConfig(filters)` to `config.py`'s `SOURCES`, importing it
from `src.job_sources.web3_career.web3_career`.

## Tests

Run the API source tests without Telegram or database access:

```shell
python -m unittest discover -s tests
```
