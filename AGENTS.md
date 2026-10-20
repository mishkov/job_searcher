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

- Don't write documentation that just copied implementation details.
- Don't add any work/implementation documentation to the README.md. README is like welcome page, it can contain only general info like "python script periodically checking arbitary job sources".
- It's good practice to add setup steps to README if they are required/needed to run/support some source. If source does not require any pre-setup like api key then ignore that and don't add to README text like "Source X does not require API key".