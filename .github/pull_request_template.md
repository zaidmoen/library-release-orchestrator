## What

<!-- What changes, and why -->

## Safety checklist

- [ ] No new code path merges, force-pushes, deletes branches, deploys or overwrites a version
- [ ] New subprocess calls use argv lists (no shell) and go through `gitutil.repo.Git` for Git
- [ ] Untrusted input (tickets, commits, PR text) is never executed or used as a path or command
- [ ] Write operations stay behind `--approve`
- [ ] Docs (`docs/`, README, Skill references) match the behaviour

## Evidence

- [ ] `ruff check` and `ruff format --check` pass
- [ ] `mypy --strict src/release_orchestrator` passes
- [ ] `pytest -q` passes (paste the summary line)
