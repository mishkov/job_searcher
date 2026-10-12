import hashlib

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
    def __init__(self, name: str, filters: SearchFilter):
        super().__init__(name, filters)

    async def fetch(self, since: datetime) -> list[Job]:
        jobs: list[Job] = []
        for url in self._collect_job_urls(since):
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

            has_exclude_keyword = False
            for keyword in self.filters.exclude_keywords:
                if keyword in description:
                    has_exclude_keyword = True
                    break
            if has_exclude_keyword:
                continue

            formatElement = soup.select_one('#body > section > article.row.vacancy > section > header > div.jobinfo > span.jobformat')
            if formatElement is None:
                raise ValueError('Work format not found')
            formatElementText = formatElement.get_text(strip=True)
            isRemoveAvailabe = 'Удаленная работа' in formatElementText 

            salaryElement = soup.select_one('#body > section > article.row.vacancy > section > header > div.jobinfo > span.salary')
            if salaryElement is None:
                raise ValueError('Salary not found')
            salary = salaryElement.get_text(strip=True)

            jobs.append(Job(
                vacancy_name,
                description,
                salary if len(salary) != 0 else '-',
                isRemoveAvailabe,
                url,
                hashlib.sha256(url.encode()).hexdigest(),
            ))

        return jobs

    def _collect_job_urls(self, since: datetime) -> list[str]:
        jobs_urls: list[str] = []

        if '*' in self.filters.keywords:
            return self._collect_job_urls_for_keyword(since, '*')
        
        for keyword in self.filters.keywords:
            jobs_urls += self._collect_job_urls_for_keyword(since, keyword)

        return jobs_urls

    def _collect_job_urls_for_keyword(self, since, keyword: str) -> list[str]:
        jobs_urls: list[str] = []

        page = 1
        has_next_page = True
        while has_next_page:
            search_url = 'https://geekjob.ru/json/find/vacancy?page=' + str(page)
            if keyword != '*':
                search_url += '&qs=%22' + keyword +'%22'

            response = requests.get(search_url)
            response.raise_for_status()
            data = response.json()

            has_next_page = data['nextpage'] > 0

            for vacancy_data in data['data']:
                if self._is_outdated(since, vacancy_data):
                    continue

                jobs_urls.append('https://geekjob.ru/vacancy/' + vacancy_data['id'])

        return jobs_urls

    def _is_outdated(self, since: datetime, vacancy_data) -> bool:
        vacancy_date = dateparser.parse(vacancy_data['log']['modify'], languages=["ru"])
        if vacancy_date is None:
            raise ValueError('Failed to parse date')
        
        return vacancy_date.date() < since.date()