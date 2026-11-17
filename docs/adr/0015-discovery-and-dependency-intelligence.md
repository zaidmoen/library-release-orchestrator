# ADR-0015: Repository discovery, PR/Jira resolution and dependency intelligence

Status: accepted (2026-11-17)

## Context

Engineering review feedback on `/library-release`:

1. Users had to name the repository and config, and give both a PR and a Jira ticket,
   although the team convention (Jira key in the PR title or branch) already links them.
2. Release state must come from the repository and GitHub, not from a hand-maintained list.
3. "rc.23 contains rc.22" (Git ancestry) was shown as a *confirmed dependency* in previews.
   Ancestry describes content. It doesn't prove that one change needs another to work.
4. Teams keep a release-dependency sheet; the dry run should end in a report that can be
   posted to Slack.

## Decision

New read-only modules, all reusing the existing adapters (Git wrapper, `GitHubReader`,
issue snapshot, `ModeEngine`, planner):

| Module | Role |
|---|---|
| `context.py` | Git top-level, remote → `owner/name`, branch, HEAD, default branch (`refs/remotes/<remote>/HEAD` → `ls-remote --symref` → GitHub API → config), nearest config *whose `library.repo` is this repository*, tags. Each value records its source; gaps are `problems[]` with the single input that resolves them. |
| `integrations/linkage.py` | Scenarios A (PR), B (ticket), C (both: mismatch = `CONFLICT`), D (neither: open PRs on the default branch are offered, never picked). PR source: live GitHub REST, or a labelled snapshot file. Keys in the title/branch are the PR's own; keys in the body are mentions. |
| `release_graph.py` | Separates **ancestry** (content) from **functional dependencies** (Jira link, code symbol proof, sheet row; cherry-pick probe = INFERRED). Heuristic file/symbol overlaps are a third, undirected list that can never form a cycle or block. Classification: CONFIRMED / INFERRED / UNKNOWN / BLOCKED. Detects missing prerequisites, cycles (over evidence-backed edges only), conflicting requested order, candidates off the source branch or stable base, and ambiguous ancestry. |
| `integrations/dependency_sheet.py` | Optional `DependencySheet` interface; CSV implementation with configurable columns. No sheet URL or schema exists in the repository, so none is assumed. |
| `reporting/release_report.py` | Dry-run report (Markdown + JSON) with final eligibility READY / BLOCKED / NEEDS REVIEW, Slack Block Kit payload and plain text, and an optional `SlackWebhookDelivery` that needs `approve=True` and a `hooks.slack.com` webhook from an env var. The CLI and the Skill never send. |
| `modes.py` | Discovery adds tags that exist on the remote but not locally, and GitHub Release metadata cross-checked with tags. Preview: ancestry moved to `dependencies.ancestry`, and only real step blockers block. |

### Source precedence

1. **Git / GitHub**: release content, ancestry and the tag set. A sheet release assignment
   that Git contradicts is a conflict; Git wins for content.
2. **Jira**: functional links. A sheet dependency Jira doesn't have is a conflict, and the
   edge is INFERRED.
3. **Code analysis**: independent proof. A code dependency without a Jira link is
   CONFIRMED and annotated "missing Jira link".
4. **Sheet**: supplementary declarations.
5. **YAML config**: providers, registry, approval policy, environments and rules only.
   Never release state.

Conflicts are always reported. INFERRED or UNKNOWN relationships, conflicts, unanswered
questions and discovery uncertainties give **NEEDS REVIEW**, never READY.

## Consequences

* `/library-release` works with no `--config` from inside the library repository (or any
  subdirectory). `--repository owner/name` is the only override that's ever needed, and
  only when the remote can't identify the repository.
* All new commands (`context`, `resolve-change`, `release-graph`, `release-report`) are
  read-only. The report refuses a stale preview (`STALE_PREVIEW`).
* Live Jira isn't read directly: the issue snapshot (ADR-0013) gains an optional
  `pull_requests` (Development panel) list.
