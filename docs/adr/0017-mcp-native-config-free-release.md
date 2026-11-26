# ADR-0017: MCP-native evidence, configuration-free start, consumers, branches, release notes, safe reordering

Status: accepted (2026-11-23)

## Context

Engineering feedback after the Phase 1 demo asked for six things:

1. Run `/library-release` inside any library repository without writing a YAML file first.
2. Use GitHub and Jira through the MCP tools engineers already have in Claude Code.
3. Find consumer services from the library README first, then verify them.
4. Show which branch and PR a dependency update would use before any write.
5. Generate GitHub Release notes from the *actual* release content.
6. Never rewrite release tags when releases are reordered.

## Decisions

### 1. MCP evidence is ingested by the engine, never interpreted by Claude

The Python engine can't call MCP tools; Claude Code can. The Skill calls the
**read-only** tools and passes the raw JSON to `mcp-ingest`. The engine then:

* validates and normalises each payload into a typed record (`integrations/mcp_snapshot.py`);
* stores it under `<state>/mcp/` with provenance (tool, `fetched_at`) and hashed file names;
* serves it to providers that **prefer** fresh MCP evidence:
  * `McpPullSource` before snapshot files and REST;
  * `McpIssueTracker` layered over the Jira fixture.

The normalisers were built against **real** responses of `list_branches`,
`list_pull_requests` and `get_file_contents`, captured on 2026-11-23 from this
repository. Jira follows the Jira REST v3 issue schema, which the Atlassian tools return.
That schema wasn't verified against a live FIN ticket, because the lab tickets are
fictional.

**Unavailable and stale evidence is reported, never treated as "nothing found":**
* an unreachable PR source becomes `unavailable (CODE)` plus a warning;
* Jira evidence older than `max_age_minutes` (default 30) is marked stale.

The available GitHub MCP tools have **no** release-write operation. GitHub Release notes
are therefore written through REST with `GITHUB_TOKEN`, as a draft only (decision 5).

### 2. Configuration-free runtime config (`bootstrap.py`)

**Derived from trusted local facts**, each with a recorded source:
* library name, package directory, version file and tag prefix;
* source branch;
* README-verified consumers;
* the allowlist from the `origin` remotes.

The derived config is validated by the **same** `parse_config` as YAML. An audit copy is
written to `<git-common-dir>/lro-state/runtime-config.json`, which is never committed.

**Never inferred:**
* validation commands;
* production publishing (the registry is `local`);
* environments;
* approval policies.

Validation commands come from YAML, or from the engineer through
`trust-validation --approve`. That command is validated exactly like YAML commands
(allowlisted placeholders) and stored in `<state>/trusted-validation.json`. The engine
may *suggest* a command for display; it never adopts one.

An explicit or discovered YAML always wins.

### 3. README-first consumer discovery (`consumers/readme_discovery.py`, `consumers/manifests.py`)

**Candidates** come from GitHub URLs and from list items under consumer-like headings.
README text only proposes names.

**Verification** reads the candidate's real manifests:
* `requirements*.txt`;
* PEP 621 and Poetry `pyproject.toml`;
* `setup.cfg`.

It reads them from a local clone (whose `origin` must match a URL the README gives), or
from GitHub MCP `get_file_contents` evidence. Lock files and `setup.py` are reported as
unsupported, never guessed.

**Statuses:**
* CONFIRMED;
* NOT_A_CONSUMER;
* POSSIBLE (an unverified search hit);
* INACCESSIBLE;
* UNKNOWN.

Discovery is `complete` only when every candidate is resolved. Inaccessible repositories
are never treated as unaffected.

If the README lists nobody, the engine returns the agreed Arabic question and targeted
`search_code` queries (manifests and imports, scoped to the owner), never an org-wide
crawl.

### 4. Impact matrix (`consumers/impact.py`)

Built on the planner's existing per-consumer analysis. Its decisions:
* `UPGRADE_RECOMMENDED`;
* `BREAKING_IMPACT` (imports a symbol the release removes);
* `NOT_REQUIRED_STATIC` (static only; runtime not verified);
* `ALREADY_COMPATIBLE`;
* `MANUAL` (not an `==` pin, or an unsupported format);
* `UNRESOLVED` (not readable, or declared-only);
* `NOT_A_CONSUMER`.

Each row carries the affected modules, the evidence, the required action and the
validation status recorded by `update-consumers`.

### 5. Branch and PR strategy (`branch_plan.py`)

**Ticket-linked branches** are found as whole tokens (FIN-204 matches `feature/FIN-204-x`,
never `FIN-2040`) in local refs, remote-tracking refs and GitHub MCP evidence. Each one is
classified as merged, active, stale or unverified.

**Strategies:**
* `new`: a deterministic branch from the target;
* `stack`: a *new* update branch on an active feature branch, with its Draft PR into that
  branch. The feature branch is never modified; the Git guard forbids moving refs anyway;
* `separate`;
* `ambiguous`: the engineer must choose;
* `existing`: an idempotent re-run.

Duplicate PRs found in GitHub evidence are reported.

**Execution** uses the existing approval-gated `update-consumers`. The confirmed target is
explicit: `--base <service>=<branch>`, and unknown services are refused.

### 6. Release notes (`reporting/release_notes.py`)

Built from `plan.included` only. Excluded changes are kept as reviewer metadata and never
appear in the notes.

**Sources:**
* sections from Conventional Commit types, including after a ticket prefix (`FIN-204: feat(...)`);
* breaking changes only from `!:` or a `BREAKING CHANGE:` footer, or from a detected public
  API removal;
* PR references only from PR evidence;
* consumer impact from the matrix;
* validation and publication from the run state.

**`publish-release-notes` gates:**
* approval;
* the artifact is published;
* the local tag is at the release commit;
* the remote tag is at the same commit.

It then creates or updates a **draft** GitHub Release. A published release is never
modified (`RELEASE_ALREADY_PUBLISHED`).

### 7. Safe reordering

The existing invariants are kept, and a new one is added:

**Kept:**
* the Git guard refuses `tag -d` / `tag -f`;
* `publish` refuses a tag that points elsewhere (`VERSION_COLLISION`);
* pushes are never forced;
* RC numbers increase monotonically.

**New:** a valid reorder allocates a **new candidate** (`new_candidate`, the next free
`rc.N`, which skips any number already used by any tag) for the first release in the new
sequence. Existing candidates and their tags stay as they are.

Tests assert that a full prepare → validate → publish of a reordered candidate only
*adds* one tag, and that a duplicate publish changes nothing.

## Consequences

* `/library-release` works in a library repository with no YAML. Plans stay blocked until
  the engineer confirms a validation command, which is deliberate.
* GitHub and Jira data comes from MCP when the tools are available. Each source states
  whether it is live MCP, a snapshot, REST or unavailable.
* Remote writes (branches pushed, PRs, the GitHub Release) remain behind explicit
  approvals. The GitHub Release is draft-only.
* **Not done:**
  * live Jira for the lab (fictional tickets);
  * a GitHub MCP release-write tool (none exists here);
  * automatic consumer cloning (the engineer provides local clones or MCP manifest
    evidence);
  * import analysis for remote-only consumers (declared-only, marked UNRESOLVED).
