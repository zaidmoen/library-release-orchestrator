# ADR-0013: Issue-tracker eligibility from timestamped snapshots

* Status: Accepted
* Date: 2026-11-10

## Context

Jira knows things Git doesn't: whether a ticket is ready, and which tickets block which.
The Atlassian MCP server available in this environment is connected to the company's real
Jira site with read-write access. The lab tickets (FIN-*) are fictional and don't exist
there. Creating them would write to a production company system, and copying real
tickets into this public repository would leak internal data. The engine also can't call
MCP tools itself, and shouldn't hold Jira credentials.

## Decision

* The engine reads an **issue snapshot** (`issue_tracker.fixture`): issues with `status`,
  `blocked_by` links, `consumers`, `accessible`, plus a top-level `fetched_at`. In
  production, Claude (or a CI job) refreshes the snapshot via the Jira MCP/REST API. In the
  lab it's a checked-in fixture.
* `check_eligibility` (deterministic) blocks the plan on:
  * `ISSUE_DATA_STALE`: snapshot older than `max_age_minutes`, or undated;
  * `ISSUE_INACCESSIBLE`: an issue the snapshot couldn't read;
  * `ISSUE_NOT_RELEASABLE`: status not in `release_statuses`;
  * `UNRESOLVED_BLOCKER`: a `blocked_by` issue that is neither in this release, already
    released, nor resolved;
  * `ISSUE_LINK_CYCLE`: a cycle in `blocked_by` links.
* Code analysis and tracker links are **independent evidence**. Either one can block. If
  the code depends on a change the tracker doesn't link, the plan records a *traceability
  gap* risk. Neither source is silently ignored.
* Ticket text stays untrusted. Only keys matching the configured pattern, statuses and
  allowlisted consumer names are read.

## Consequences

* FIN-211 is now blocked by both sources (`MISSING_PREREQUISITE` from code and
  `UNRESOLVED_BLOCKER` from the tracker).
* **Live Jira is BLOCKED**: it needs a sandbox Jira project, or explicit authorization for
  read-only checks against a designated test project.
