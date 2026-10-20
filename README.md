# Job Searcher

A Python Telegram bot for checking configured job sources and sharing matching
vacancies.

## Setup

Copy the example configuration and edit `config.py` to choose sources and search
filters:

```shell
cp config.example.py config.py
```

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
