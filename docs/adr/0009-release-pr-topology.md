# ADR-0009: Library release PRs target a release integration branch, not the trunk

* Status: Accepted
* Date: 2026-11-08
* Context source: live run on `zaidmoen/harri-release-lab-library`

## Context

In the live run, `release/1.1.0` was cut from `v1.0.0` and holds three commits: FIN-204
(two cherry-picks) and the version bump. `main` already contains FIN-198 through FIN-230.
GitHub reported the two branches as **diverged**: 8 behind and 3 ahead, merge-base
`v1.0.0`. A PR from `release/1.1.0` into `main`:

* **as a review**, shows a misleading diff. GitHub's three-dot diff happens to show only
  the release delta, but the PR's mergeability, conflicts and checks are computed against
  the trunk, which the release deliberately doesn't contain.
* **if merged**, would add nothing but a version bump to `main`, set `__version__` on trunk
  to an out-of-order version, and could conflict with later work on `__init__.py`.

## Options

| Option | Diff shown | Safe to merge? | Notes |
|---|---|---|---|
| Review-only draft PR into `main` | Release delta (three-dot), with trunk context for checks | **No.** Must never be merged | What the POC did. Relies on people remembering "never merge" |
| Mergeable PR into `main` | — | No | Semantically wrong for out-of-order releases |
| **PR into a release integration branch** `release-base/<base-tag>` pinned at the release base | Exactly the release commits | Yes, merging only advances a release line | Chosen |
| Per-major release line (`release-line/1.x`) | Release delta since the last release on that line | Yes | Production evolution of the option above. Needs ownership of the line branch |

## Decision

* `library.release_pr_target` defaults to **`integration`**. On `pr`, the orchestrator
  creates `release-base/<base_ref>` at the plan's base SHA if it's missing. It **refuses**
  (`INTEGRATION_BRANCH_MISMATCH`) if the branch exists anywhere else, and never moves it.
  `push` sends it with the other refs. The PR base is that branch.
* `source` keeps the old behaviour for teams that explicitly want a trunk-targeted,
  review-only PR.
* An external PR request that hasn't been submitted yet is corrected in place when its
  base changes. A recorded PR is never silently retargeted.
* The library's `main` is never a merge target for out-of-order releases. Consumer
  dependency PRs still target each service's own `main`, because those really are
  meant to be merged by the service team.

## Consequences

* The PR's "files changed" equals the release content: FIN-204 plus the version bump.
* Each release base gets its own `release-base/*` branch. In production, replace these
  with one long-lived release line per major version owned by release engineering, and
  publish from that line in CI after merge.
