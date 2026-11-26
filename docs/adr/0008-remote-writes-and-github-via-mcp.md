# ADR-0008: Approved remote writes behind a sandbox allowlist; PRs through the GitHub MCP server

* Status: Accepted
* Date: 2026-11-08
* Supersedes the "never pushes" part of ADR-0003 and ADR-0006

## Context

To reach GitHub, release and dependency branches must exist on the remote. The original
engine forbade every `push`. This environment has no `GITHUB_TOKEN`, but it does have
the GitHub MCP server, which shows the user a form for every PR it creates.

## Decision

1. **Push is allowed only through `Git.push_branch`**, which:
   * pushes exactly one `refs/heads/<branch>` or `refs/tags/<tag>` ref, without `+`, so it
     is never forced. Git rejects non-fast-forward updates.
   * refuses unless the remote URL matches `remote_allowlist` in the trusted config
     (normalised `github.com/<owner>/<repo>`, exact match).
   * runs only from the `push` CLI step, which needs `--approve`.
   * refuses to push a tag that already exists on the remote at another commit.
   The general Git guard still forbids `push`, so no other code path can push.
2. **PR providers** (`pull_requests.provider`):
   * `local`: JSON records (tests and offline demos).
   * `github`: Python REST adapter. **MOCKED** until run with a real token.
   * `external`: the engine writes an auditable PR *request*
     (`<state_dir>/pr-requests/*.json`) and returns `awaiting_external_creation`. Claude
     creates the draft PR through the GitHub MCP server (the user submits the form), then
     records the result with `record-pr`. Re-running returns the recorded PR, so there are
     no duplicates.
3. **PR status** is read back through MCP (`pull_request_read`) and stored as evidence.
   The engine never merges. No provider has a merge method.

## Consequences

* Live PR creation is verified through MCP, not through the Python REST adapter. The
  docs keep these two apart.
* A compromised plan file can't redirect pushes, because the allowlist lives in config.
* Credentials stay with Git's credential manager and the MCP server. The engine never
  reads them.
