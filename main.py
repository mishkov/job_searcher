import os
from dotenv import load_dotenv
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
import requests
from bs4 import BeautifulSoup
import dateparser

@dataclass
class Job:
    title: str
    description: str
    salary: str
    is_remote_allowed: bool


@dataclass
class SearchFilter:
    keywords: list[str]

class JobsSource(ABC):
    @abstractmethod
    def fetch(self, since: datetime, search_filter: SearchFilter) -> list[Job]:
        pass

class GeekJobRuJobsSource(JobsSource):
    def fetch(self, since: datetime, search_filter: SearchFilter) -> list[Job]:
        jobs_urls: list[str] = []
        for keyword in search_filter.keywords:
            page = 1
            has_next_page = True
            while has_next_page:
                response = requests.get('https://geekjob.ru/json/find/vacancy?page=' + str(page) +'&qs=%22' + keyword +'%22')
                response.raise_for_status()

                data = response.json()

                has_next_page = data['nextpage'] > 0

                vacancies_data = data['data']
                for vacancy_data in vacancies_data:
                    vacancy_date = dateparser.parse(vacancy_data['log']['modify'], languages=["ru"])
                    if vacancy_date is None:
                        raise ValueError('Failed to parse date')

                    if vacancy_date.date() < since.date():
                        continue

                    vacancy_id = vacancy_data['id']

                    jobs_urls.append('https://geekjob.ru/vacancy/' + vacancy_id)

        jobs: list[Job] = []
        for url in jobs_urls:
            response = requests.get(url, timeout=15)
            soup = BeautifulSoup(response.text, "html.parser")

            element = soup.select_one("h1")
            if element is None:
                raise ValueError('Vacancy title not found')
            vacancy_name = element.get_text(strip=True)

            text = soup.get_text(separator="\n", strip=True)
            description = (
                text.split("Описание вакансии", 1)[1]
                    .split("Специализация", 1)[0]
                    .strip()
            )

            formatElement = soup.select_one('#body > section > article.row.vacancy > section > header > div.jobinfo > span.jobformat')
            if formatElement is None:
                raise ValueError('Work format not found')
            formatElementText = formatElement.get_text(strip=True)
            isRemoveAvailabe = 'Удаленная работа' in formatElementText 

            salaryElement = soup.select_one('#body > section > article.row.vacancy > section > header > div.jobinfo > span.salary')
            if salaryElement is None:
                raise ValueError('Salary not found')
            salary = salaryElement.get_text(strip=True)

            jobs.append(Job(vacancy_name, description, salary if len(salary) != 0 else '-', isRemoveAvailabe))

        return jobs

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
