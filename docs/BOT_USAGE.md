# Running the bot

Keep the bot process running to receive vacancies automatically. Set
`FETCH_PERIOD_IN_MINUTES` in `config.py` to a positive number; the interval starts
after each check finishes.

The bot uses one shared monitor and global search history. Send `/start` in a
private chat to select that chat for vacancy delivery; no user-ID configuration
is required. Commands from any private chat control the same monitor. A later
`/start` from a different private chat selects it for subsequent checks; a check
already running finishes delivering to its original chat. Group commands are ignored.

## Commands

- `/start` enables automatic checks and searches immediately, unless sleeping.
  Repeating it while enabled leaves the current schedule alone.
- `/stop` pauses checks. A source already running finishes fetching and delivering
  its vacancies before the bot pauses.
- `/status` shows whether monitoring is paused, sleeping, searching, or waiting;
  the current source, next check, and last check result are included.
- `/setsleeptime` asks for one daily period in `HH:MM-HH:MM` format, such as
  `23:00-08:00`. Times use **Europe/Minsk**. Overnight periods are supported.
  Send `off` to disable sleeping time or `/cancel` to keep the current setting.
  Start and end must differ. `/start`, `/stop`, and `/status` also cancel a pending
  sleep-setting prompt.

Sleep starts inclusively and ends exclusively: `23:00-08:00` pauses checks at
23:00 and resumes with a catch-up check at 08:00. A source already running can
still send vacancies after the sleep boundary while it finishes. `/stop` keeps
monitoring paused after sleeping time ends. Changing sleeping time prompts a new
check when monitoring is enabled and awake.

The delivery chat, monitoring, and sleeping settings survive application restarts
in the existing database. The first launch is paused until `/start`. A restart resumes the saved
schedule; an overdue check runs immediately when awake. A restart during a check
retries it using saved source checkpoints and delivered-vacancy records.
If no delivery chat has been saved yet, send `/start` once to select one.

Automatic runs send new vacancies without progress messages or empty-search
notifications. `/status` includes failed-source counts; detailed diagnostics go
to the application logs. Failed checks are retried at the normal interval.

Sleeping and paused periods do not advance source checkpoints. Sources without
history use a seven-day lookback, subject to [source coverage](SOURCES.md).
Keep `IS_DEBUG_MODE` disabled for normal use: debug mode ignores history and can
send the same vacancies again on every check.

## Job posts

Click **✅ Applied** on a vacancy to save its displayed post and replace it with
`✅ Applied [TITLE]`. **Open job 🔗** remains available. Click **Restore post** to
bring back the original message, formatting, and buttons. Saved posts survive
bot restarts in the database; restoration includes the description excerpt that
was displayed, not the full description on the job website. Restoring a post
does not cause the monitor to deliver that vacancy again.
