# ADR-0003: Isolation with detached worktrees (write) and sandbox clones (read)

* Status: Accepted
* Date: 2026-11-02

## Context

Preparing a release must never disturb the developer's checkout. Dry-run must be
truly non-mutating, and even `git worktree add` writes into `.git/worktrees`.

## Decision

* **Read-only operations** (analysis, `prepare --dry-run`): `git clone --no-checkout`
  into a temporary directory. Probes happen there and the clone is deleted.
* **Approved prepare:** `git worktree add --detach <tmp> <base>`, then
  `cherry-pick -x` for each commit, then a version-bump commit, and only then
  `git branch <release-branch> <sha>`. On failure the step runs
  `cherry-pick --abort` and `worktree remove --force` on **its own temporary
  worktree**. No branch was created, so no branch ever needs deleting.
* **Validation:** `git archive <sha>` extracted to a temporary directory, so tests
  see exactly the committed tree and can't leave files behind in the repository.

## Consequences

* The tool never needs `reset --hard`, `stash`, `push --force` or branch deletion.
* Costs extra disk space for clones. Acceptable for library-sized repositories.
  Large monorepos could use partial clones (`--filter=blob:none`) **[planned]**.
