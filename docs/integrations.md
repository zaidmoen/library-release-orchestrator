# External integrations

Each external system is behind one small adapter. The engine verifies authoritative state
and never assumes it.

| System | Adapter | Access | What it is used for | Verification level |
|---|---|---|---|---|
| Git remotes (GitHub) | `gitutil.repo.Git` | Git credential manager (user) | `ls-remote` (refs, PR refs), allowlisted never-forced `push` | **Verified live** on `harri-release-lab-*` |
| GitHub PR existence | `integrations.pr_reconcile` | same Git credentials | discover/verify PRs via `refs/pull/<n>/head` (ADR-0010) | **Verified live** (read-only): PR #1/#2 on this repo; lab reports `not_created` |
| GitHub PR status, reviews, checks | `integrations.github_status.GitHubReader` | `GITHUB_TOKEN` (optional for public repos) | approval policy gate for `publish` (ADR-0011) | **Verified live** (read-only) on PR #2 of this repo. Private lab repos: **blocked** (no token, and the connector has no access) |
| GitHub PR creation | external (Claude via GitHub MCP, or a human via compare link) + `record-pr`/`reconcile-prs` | MCP app / user | draft PRs | PR #2 on this repo was created through the MCP form. Lab PRs **blocked** (private repos on GitHub Free have no drafts; repos stay private) |
| GitHub REST PR provider | `integrations.github.GitHubPullRequestProvider` | `GITHUB_TOKEN` | draft PR creation from Python | **Mocked** (fake transport); no token available |
| Package index (PEP 503) | `versioning.pypi.PypiRegistry` | `username_env`/`password_env` | immutable wheel publication, read-after-write (ADR-0012) | **Verified locally** against a real, isolated pypiserver. Production index: not exercised (needs credentials and authorization) |
| Local registry | `versioning.registry.LocalRegistry` | filesystem | lab/demo publication | Verified locally |
| Issue tracker | `integrations.jira.FixtureIssueTracker` (snapshot) | snapshot file | eligibility: status, blockers, cycles, staleness (ADR-0013) | **Verified locally**. Live Jira **blocked** (the connected site is the company's production Jira; not touched) |
| CI | `GitHubReader.check_runs` | as above | required checks in the approval policy | **Verified live** (read-only) on PR #2 |

## Required permissions (least privilege)

| Credential | Scope | Used by |
|---|---|---|
| Git credentials for sandbox remotes | contents: read/write on the allowlisted repos only | `push`, `ls-remote` |
| `GITHUB_TOKEN` (fine-grained) | Pull requests: read, Checks: read, Metadata: read | `pr-status`, `github-pr-status`, `publish` gate |
| Registry account | upload to the release index only | `publish` |
| Jira (snapshot refresh) | read-only on the release project | outside the engine (Claude/CI) |

No adapter has merge, delete, deploy or visibility-change capabilities.

## Failure semantics

| Category | Examples | Behaviour |
|---|---|---|
| Transient | DNS, timeouts, resets, HTTP 5xx, rate limit | bounded retries (3, exponential), then `IntegrationError` with `retryable: true` |
| Permanent | 401/403/404, rejected non-fast-forward, missing credentials | fail immediately, nothing written |
| Ambiguous | lost response after a write | reconcile against authoritative state (remote ref, index) before deciding |
| Conflict | version exists with another digest, tag points elsewhere, PR head moved | explicit `BLOCKED` with evidence, never overwritten |
| Concurrency | two processes on one run | `CONCURRENT_MODIFICATION`; reload and retry |
