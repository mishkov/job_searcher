from dataclasses import asdict
from datetime import datetime

from src.error_reporting import ErrorReporter, report_errors
from src.job import Job
from src.job_sources.job_source import JobsSource, JobsSourceConfig


def source_error_context(config: JobsSourceConfig) -> dict[str, object]:
    # Explicitly select configuration fields; some providers also store API tokens.
    return {
        'source': config.name,
        'source_type': type(config).__name__,
        'filters': asdict(config.filters),
    }


class ErrorReportingJobsSource(JobsSource):
    """Add reporting to any provider without changing its exception contract."""

    def __init__(self, source: JobsSource, reporter: ErrorReporter):
        super().__init__(source.config)
        self._source = source
        self._reporter = reporter

    async def fetch(self, since: datetime) -> list[Job]:
        with report_errors(self._reporter, **source_error_context(self.config),
                           since=since.isoformat(), operation='fetch_jobs', posting=None):
            return await self._source.fetch(since)
