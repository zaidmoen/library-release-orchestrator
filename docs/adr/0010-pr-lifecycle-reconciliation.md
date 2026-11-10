# ADR-0010: PR lifecycle reconciliation through Git refs; bounded retries for network Git

* Status: Accepted
* Date: 2026-11-10
* Context source: live validation on `harri-release-lab-*` (docs/live-github-validation.md)

## Context

The live run showed that PR existence was only as reliable as a human running
`record-pr`. Three things went wrong:

* The GitHub MCP connector couldn't see the private lab repos (404), so it could neither
  create nor read PRs there.
* PRs submitted through the browser left no record in the engine. A later run could prepare
  the same PR again.
* Nothing detected drift: a recorded PR that was deleted or retargeted, or whose head no
  longer matched the release branch.

GitHub advertises every PR as `refs/pull/<n>/head` to any client that can read the
repository. The Git credentials already used for pushing can therefore observe PR existence
and head commits without an API token.

## Decision

1. `release-orchestrator reconcile-prs --plan …` (read-only towards the remote) compares each
   PR request (library and prepared consumers) with the remote's `refs/pull/*/head`.
   Outcomes:

   | Status | Meaning | Action |
   |---|---|---|
   | `not_created` | No PR has the branch's head commit | Safe to create one |
   | `discovered` | Exactly one PR has it | Recorded automatically; do **not** create another |
   | `recorded_verified` | The recorded PR exists and points at the branch head | Nothing to do |
   | `ambiguous` | Several PRs have it | Not recorded; a human picks one (`record-pr`), duplicates are reported |
   | `recorded_head_moved` | The recorded PR's head differs from the branch | Investigate; exit 3 |
   | `recorded_missing` | The recorded PR number isn't on the remote | Investigate; exit 3 |
   | `branch_not_pushed` | The head branch isn't on the remote | Run `push` first |

2. The Skill must run `reconcile-prs` immediately before creating any PR, and create one only
   on `not_created`.
3. Network Git operations (`ls-remote`, `push`) retry **transient** failures (DNS, timeouts,
   connection resets, HTTP 5xx) up to 3 times with exponential backoff. Permanent failures
   (rejected non-fast-forward, auth, not found) are never retried. Before each push retry
   the remote ref is re-read, so a push whose response was lost is never repeated.

## Consequences

* PR existence and head identity are **verifiable live** with existing credentials. The
  discovery was verified against `zaidmoen/library-release-orchestrator` (PR #1).
* Git refs can't show open, closed, draft or merged state (closed PRs keep their refs).
  Discovered PRs are recorded as `exists (discovered via git)`. Status gates (for example
  "approved") still need the GitHub API: MCP with repo access, or a token.
* Retries are bounded and synchronous. There is no background retry queue.
