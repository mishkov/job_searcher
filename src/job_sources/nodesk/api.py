from abc import ABC, abstractmethod


class NoDeskFetchError(RuntimeError):
    pass


class NoDeskApi(ABC):
    @abstractmethod
    def get_jobs_page(self, page: int, page_size: int) -> dict:
        """Return an unfiltered Algolia result page."""
        pass

    @abstractmethod
    def get_job_posting(self, url: str) -> dict | None:
        """Return JobPosting data, or None for a removed vacancy."""
        pass
