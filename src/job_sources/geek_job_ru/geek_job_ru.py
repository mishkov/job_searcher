from dotenv import load_dotenv
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
import requests
from bs4 import BeautifulSoup
import dateparser

from src.job_sources.job_source import JobsSource
from src.search_filter import SearchFilter
from src.job import Job

class GeekJobRuJobsSource(JobsSource):
    async def fetch(self, since: datetime, search_filter: SearchFilter) -> list[Job]:
        jobs: list[Job] = []
        for url in self._collectJobUrls(since, search_filter):
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

            jobs.append(Job(vacancy_name, description, salary if len(salary) != 0 else '-', isRemoveAvailabe, url))

        return jobs

    def _collectJobUrls(self, since: datetime, search_filter: SearchFilter) -> list[str]:
        jobs_urls: list[str] = []
        
        for keyword in search_filter.keywords:
            page = 1
            has_next_page = True
            while has_next_page:
                response = requests.get('https://geekjob.ru/json/find/vacancy?page=' + str(page) +'&qs=%22' + keyword +'%22')
                response.raise_for_status()
                data = response.json()

                has_next_page = data['nextpage'] > 0

                for vacancy_data in data['data']:
                    if self._isOutdated(since, vacancy_data):
                        continue

                    jobs_urls.append('https://geekjob.ru/vacancy/' + vacancy_data['id'])

        return jobs_urls

    def _isOutdated(self, since: datetime, vacancy_data) -> bool:
        vacancy_date = dateparser.parse(vacancy_data['log']['modify'], languages=["ru"])
        if vacancy_date is None:
            raise ValueError('Failed to parse date')
        
        return vacancy_date.date() < since.date()