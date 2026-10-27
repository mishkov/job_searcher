# Source coverage

## Betera and MaxLine

These sites do not publish vacancy dates, so the first check includes all active
matches regardless of the usual seven-day lookback. Saved job history prevents
repeat deliveries. Betera's general CV submission listing is excluded.
MaxLine advertises office work in Minsk.

## Ashby

`AshbyJobsSourceConfig(company_slug, filters)` accepts the board slug from the
job URL: `gismart` for `https://jobs.ashbyhq.com/gismart/...`. Use a separate
entry for each company; each board and filter combination has independent search
history. Date filtering uses the last publication date. Unlisted jobs are excluded.
Hybrid roles allow remote work but retain their workplace type and locations in
the description.

## Comeet

`ComeetJobsSourceConfig(company_slug, company_uid, filters)` requires both company
identifiers from the board URL: `biocatch` and `03.00E` for
`https://www.comeet.com/jobs/biocatch/03.00E`. A slug alone is insufficient.
Use a separate entry for each company; each board and filter combination has
independent search history. Date filtering uses the job's last update date.

## Greenhouse

`GreenhouseJobsSourceConfig(company_slug, filters)` accepts the board slug from
the job-board URL: `capitalontap` for
`https://job-boards.greenhouse.io/capitalontap`. Use a separate entry for each
company; each board and filter combination has independent search history.
Date filtering uses the publication date when available, otherwise the last
update date.

## AJ Bell techHub

Checks the [tech vacancies](https://www.ajbell.co.uk/group/techhub/vacancies).
These vacancies have no publication dates, so the first check includes all
currently advertised matches regardless of the usual seven-day lookback.
Saved job history prevents repeat deliveries on later checks.

## Telegram

For channels that append promotional text, set `ignored_text_fragments` on
`TelegramChannelJobsSourceConfig`:

```python
TelegramChannelJobsSourceConfig(
    "channel_name",
    SearchFilter(keywords=["flutter"]),
    ignored_text_fragments=[
        "Flutter & Dart | Мобильный трудоголик - Канал про разработку на Flutter.",
    ],
)
```

Fragments are literal plain text, without Markdown formatting markers. Matching
ignores case but preserves whitespace and punctuation. Fragments are removed in
the configured order before checking both inclusion and exclusion keywords;
keyword occurrences elsewhere still count. Delivered messages keep their original
text. Changing fragments starts a fresh lookback with the new rules.

## SmartRecruiters

Searches cover at most 100 results per keyword. Broad keywords, wildcard searches,
long gaps between searches, and jobs absent from SmartRecruiters' global search
can therefore miss vacancies. Use specific keywords and regular searches when
relying on this source.
