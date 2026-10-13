# Cache implementation policy

Configure a disposable cache directory in `.env`:

```dotenv
CACHE_PATH=./state/cache
```

`CACHE_PATH` must contain only cache data. Everything beneath it is safe to
delete, including while the bot is running. Leave it unset or blank to disable
caching. Relative paths are resolved from the bot's working directory. Do not
put credentials, configuration, search timestamps, seen-job records, or other
authoritative data in this directory, and never commit cache contents to Git.

## Architecture

Keep caching in an API proxy, outside domain objects, job filtering, search
history repositories, and unrelated sources. Inject the API interface into the
source. Compose the real API and optional cache proxy at application startup,
after `.env` has been loaded.

For NoDesk, `NoDeskApi` defines listing and detail operations.
`PythonRequestsModuleNoDeskApi` performs HTTP requests and extracts provider
data. `SqlCachedNoDeskApi` wraps another `NoDeskApi` and stores reusable detail
responses in SQLite. `NoDeskJobsSource` depends only on `NoDeskApi`; it has no
knowledge of cache paths, SQL, TTLs, or environment variables.

## Storage and lifetime

- Namespace files by provider below `CACHE_PATH`. NoDesk uses
  `nodesk/details-v1.sqlite3`; version the cache format when its shape changes.
- Cache successful, parsed detail responses by their listing URL for 24 hours.
  A changed URL naturally produces a cache miss. Store unfiltered provider data
  so every search applies its current date, keyword, and exclusion filters.
- Keep Algolia listing pages fresh on every run. Advertised vacancies follow
  the same filtering rules; the advertisement flag is not an exclusion rule.
- Persist each successful detail response immediately. A later network failure
  must not discard earlier cache entries. Do not cache errors or missing jobs.
- Expired entries are misses and are pruned during successful cache writes.
  Do not serve expired responses when the upstream API fails.

## Deletion and failures

Treat caching as an optional optimization. Missing directories, deleted files,
invalid cached payloads, corrupt databases, permission failures, and database
locks must fall back to the real API. A failed cache write must still return the
successful API response. Do not hide upstream failures or alter search-history
behavior to accommodate caching.

Recreate cache directories and schema lazily on writes. Open and close database
connections per cache operation; do not keep a connection to a file that may
have been deleted. Handle deletion races during both reads and writes. Avoid
creating files on a cache read and keep database lock waits short. Use bound SQL
parameters and atomic transactions so concurrent bot requests cannot observe
partially written cache entries. Never delete user files to repair a cache.

## Verification

Test cold and warm caches, reuse across source/API instances, TTL expiry,
filter changes, and successful entries surviving a later API failure. Delete
the cache directory between calls and during reads/writes. Test corrupt data,
unwritable paths, and database locks; they must behave like cache misses or
ignored writes. Verify listing discovery remains fresh and upstream errors
still propagate.
