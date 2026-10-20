# Git workflow for AI-agent

Before implementing changes, read the files under `docs/` and follow their
applicable implementation policies.

For implementation tasks:

1. Work in a separate Codex/Claude|AI-agent/git worktree.
2. pull latest main.
3. Create a branch named `codex|claude|ai-agent/<short-task-name>` from main.
4. Implement the task.
5. Run relevant tests and checks.
6. Commit all changes.
7. Push the branch to `origin`.
8. Create a pull request targeting `main`.
9. Never merge the pull request yourself.
10. Leave the worktree clean.

# Writing documentation

Documentation is top level point for understanding the project in general.

- Don't write documentation that just copied implementation details. For example following text is useless because it does not impact on how users will setup or use the script
    > Discovery follows [SmartRecruiters' global job search](https://jobs.smartrecruiters.com/).
    It searches each inclusion keyword separately, or the latest jobs for `*`, and
    checks full job descriptions against the inclusion and exclusion filters. The
    provider's keyword search determines which vacancies can be discovered.

    Add only really useful info that may affect setup/usage because if we will add details for like described above for each source then we will have large file where it will be difficult to find really important information.
- Don't add any work/implementation documentation to the README.md. README is like welcome page, it can contain only general info like "python script periodically checking arbitary job sources".
- It's good practice to add setup steps to README if they are required/needed to run/support some source. If source does not require any pre-setup like api key then ignore that and don't add to README text like "Source X does not require API key".
- Don't include instruction on how to connect each source. Single general instruction is enough so for example following text is useless
    > Add `SmartRecruitersJobsSourceConfig(filters)` to `config.py`'s `SOURCES`, importing
    it from `src.job_sources.smartrecruiters.smartrecruiters`. The example configuration
    includes it with the common filters.
- 