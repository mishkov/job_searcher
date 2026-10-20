# Source coverage

## SmartRecruiters

Add `SmartRecruitersJobsSourceConfig(filters)` to `config.py`'s `SOURCES`, importing
it from `src.job_sources.smartrecruiters.smartrecruiters`. The example configuration
includes it with the common filters.

Discovery follows [SmartRecruiters' global job search](https://jobs.smartrecruiters.com/).
It searches each inclusion keyword separately, or the latest jobs for `*`, and
checks full job descriptions against the inclusion and exclusion filters. The
provider's keyword search determines which vacancies can be discovered.

Global search returns at most 100 hits per query and currently ignores pagination
parameters. Broad keywords, wildcard searches, long gaps between searches, and
jobs absent from the global index can therefore miss vacancies. Use specific
keywords and regular searches when relying on this source.

Remote and hybrid vacancies both allow remote work; hybrid restrictions remain
in the description. Salary is left unspecified in the salary field because this
API does not provide structured pay data; any salary text remains in the full
description.
