# ADR-0006: Protocol adapters for Jira, PR hosting and registry

* Status: Accepted
* Date: 2026-11-02

## Decision

External systems sit behind `typing.Protocol` interfaces that are injected at
composition time (`workflow.Services`).

| Port | POC implementation | Status |
|---|---|---|
| `IssueTracker` | `FixtureIssueTracker` (YAML file) | Implemented and tested |
| `PackageRegistry` | `LocalRegistry` (directory) | Implemented and tested |
| `PullRequestProvider` | `LocalPullRequestProvider` (JSON records) | Implemented and tested |
| `PullRequestProvider` | `GitHubPullRequestProvider` (REST, draft PRs only, no merge method) | Implemented and unit-tested with a fake transport; **not verified against live GitHub** |
| `IssueTracker` | Jira REST | **[planned]** |
| `PackageRegistry` | Artifactory / CodeArtifact | **[planned]** |

## Consequences

The POC runs fully offline. Live integrations need credentials supplied through
environment variables and must be verified separately before anyone claims they
work.
