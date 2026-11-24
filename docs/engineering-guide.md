# Engineering guide: `/library-release` from a real library repository

This guide covers what the Skill does today (ADR-0014 to ADR-0017), how to set it up,
and what it can't do yet. Planned work is listed at the end and marked as such.

## Architecture in one picture

```
Claude Code Skill (.claude/skills/library-release)
  │  asks, explains, calls read-only MCP tools, never decides
  ├── MCP tools (GitHub, Atlassian) ──► mcp-ingest ──► <state>/mcp (validated, dated evidence)
  ▼
release-orchestrator (Python engine, deterministic)
  ├── context.py / bootstrap.py ....... repository identity, runtime config, trusted validation
  ├── consumers/ ...................... README-first discovery, manifests, impact matrix
  ├── modes.py / release_graph.py ..... RC / release / reorder options, dependency graph
  ├── planning/ + analysis/ ........... plans, AST dependency analysis, cherry-pick probe
  ├── branch_plan.py .................. branch/PR strategy per repository
  ├── workflow.py ..................... prepare → validate → PR → publish → consumers (gated)
  ├── reporting/ ...................... release notes, dry-run report, Slack text
  └── gitutil/ ........................ Git with a guard (no force, no tag delete, no merge)
```

Business rules live in Python and are unit-tested. The Skill holds no release logic, and
it can't override a BLOCKED result.

## Setup

1. **Install the engine** (once): `pip install -e .` in this repository. Link the Skill
   into Claude Code: link `.claude/skills/library-release` into `~/.claude/skills/`, or
   open Claude Code in this repository.
2. **Clone the consumer services** next to the library (same parent directory) so their
   imports can be analysed. Without a clone, a consumer can still be verified from its
   manifest through GitHub MCP, but only as "declared"; its impact stays UNRESOLVED.
3. **GitHub MCP / Atlassian MCP:** enable them in Claude Code. The Skill uses only these
   read tools: `list_pull_requests`, `list_branches`, `get_file_contents`, `search_code`,
   `getJiraIssue`.
4. **`GITHUB_TOKEN`** is optional. It's needed for:
   * private repositories through the REST fallback;
   * writing the draft GitHub Release. The MCP tools have no release-write operation.

   A fine-grained token with *Contents: read/write* on the library is enough for release
   notes. Read-only is enough for everything else.
5. **Nothing else.** No YAML is needed. If your team already has a
   `release-orchestrator.yaml`, it keeps working unchanged and always wins.

## Workflows

### First run in a repository

`/library-release` derives the runtime config (`LRO bootstrap` shows it, with the source
of each value). It asks at most two questions:

* **Confirm the validation command.** The engine suggests one, for example `python -m
  pytest -q`. Only your confirmation (`trust-validation --approve`) makes it trusted;
  until then every plan is `NO_VALIDATION_CONFIGURED`.
* **Consumers, only when the README lists none.** Search GitHub with targeted queries,
  name the repositories, or continue with coverage marked incomplete.

### Consumer discovery (README first)

The README's consumer section (headings like "Consumers", "Used by") and GitHub URLs give
candidates. Each candidate is verified against its manifests:

| Status | Meaning |
|---|---|
| CONFIRMED | a supported manifest declares the library |
| NOT_A_CONSUMER | manifests read, no declaration |
| POSSIBLE | a search hit, not verified yet |
| INACCESSIBLE | can't be read: never "unaffected" |
| UNKNOWN | only unsupported formats (lock files, `setup.py`) |

### Impact, branches and PRs

1. `LRO impact --plan P` shows the matrix: decision, affected modules, required action
   and validation status per service.
2. `LRO branch-plan --plan P` gives source and target per repository, and the
   ticket-linked branches with their state.
   * A single active feature branch gives `stack`: a new branch on top of it, Draft PR
     into it, with the feature branch untouched.
   * Several give `ambiguous`: you choose.
3. Confirm, then run `update-consumers --base <service>=<branch> --approve`. Push and the
   Draft PRs follow the existing, reconciled flow; duplicates are reported, never
   re-opened.

### Release notes

* `LRO release-notes --plan P --out notes.md` is a draft. It's built from the commits
  that ship; excluded tickets never appear, and unresolved tickets are marked.
* `LRO publish-release-notes --plan P --github-repository owner/name --approve` runs only
  when:
  * the artifact is published;
  * the tag is on the remote at the release commit;
  * you said yes.

  It creates or updates a **draft** GitHub Release and never touches a published one.

### Reordering cumulative candidates

Example: "Release rc.23's changes before rc.22's."

* If B doesn't depend on A, the plan proposes a **new** candidate (`new_candidate`, the
  next free `rc.N`) containing only B, cut from the stable base. rc.22 and rc.23 are never
  moved, deleted or re-used.
* If B depends on A, the request is BLOCKED with the evidence. You're offered: include
  the prerequisite, reorder, release only the cumulative candidate, or stop.

## Security model (what is trusted)

| Input | Trust |
|---|---|
| YAML config, `trust-validation` commands | trusted (written by an engineer) |
| Git metadata of your clones | trusted facts |
| README, Jira, PR bodies, commit messages, branch names, MCP payloads | **untrusted data**: parsed, truncated, never executed or used as a path |

All remote writes need `--approve`:
* pushes go only to allowlisted remotes and are never forced;
* PRs are drafts;
* the GitHub Release is a draft;
* there is no merge and no deploy.

Tokens are read from the environment and never printed.

## Migrating from YAML

Nothing to do: an existing `release-orchestrator.yaml` keeps working and wins over
discovery.

To move to the config-free path, delete the YAML in a branch, run `LRO bootstrap`, and
compare its `config` with your YAML. Differences are listed in `provenance`. Validation
commands must then be confirmed once with `trust-validation`. Environments and approval
policies still need YAML.

## Limitations (stated plainly)

* **Live Jira:** wasn't exercised against a real Jira site from this environment. Live
  issue data enters through `getJiraIssue` + `mcp-ingest`, and its staleness is reported.
* **GitHub MCP:** this environment's connector couldn't reach the private sandbox repos
  (404). The adapters were built on real responses from this repository.
* **Remote-only consumers:** declared-only (no import analysis) until cloned.
* **Analysis scope:** Python only. Runtime behaviour isn't verified (static analysis plus
  your test suites).
* **Final report:** doesn't yet aggregate branch decisions and release-notes status (use
  `branch-plan` and `release-notes` directly).

## Planned (not implemented)

* Sandboxed runtime verification of dependencies and verified alternatives (V5, Phase 3).
* Source-level permalinks for blockers (V5, Phase 2).
* Automatic consumer cloning, and a `doctor` command.
