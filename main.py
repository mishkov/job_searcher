import os
from dotenv import load_dotenv
from datetime import datetime

from src.search_filter import SearchFilter

from src.job_sources.geek_job_ru.geek_job_ru import GeekJobRuJobsSource

load_dotenv()

# Load config from .env. Keep config as abstract entity so we can replace the .env with some per-user record in DB
fetch_period = os.getenv("FETCH_PERIOD_IN_MINUTES")

sources = [
    GeekJobRuJobsSource()
]

for source in sources:
    jobs = source.fetch(since=datetime(2026, 9, 1), search_filter=SearchFilter(keywords=['flutter']))

    if len(jobs) == 0:
        continue

    for job in jobs:
        print(job)
