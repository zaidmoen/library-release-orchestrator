# Live demo: `/library-release` with discovery and dependency intelligence

A 10–15 minute walkthrough for the engineering team. **Nothing in it publishes, pushes,
merges, deploys, changes Jira or posts to Slack.** Every command is read-only, except
`lab-init`, which creates local fictional repositories.

## What is live and what is a fixture

| Data | Offline lab (default) | Sandbox clones (live) |
|---|---|---|
| Git history, tags, ancestry | real Git repos built by `lab-init` | `zaidmoen/zaidlibrary` sandbox |
| Code dependency (FIN-211 imports `currency.convert`) | real AST analysis | real AST analysis |
| Jira issues and links | **fixture** `jira.yaml` (fictional) | **fixture** (live Jira not connected) |
| Pull requests | **fixture** `pull-requests.yaml`, labelled `fixture` in every output | GitHub REST (`GITHUB_TOKEN` needed for private repos); the sandbox has no PRs |
| GitHub Releases | none (no remote) | GitHub REST, cross-checked with tags |
| Dependency sheet | not configured (optional CSV) | not configured |

## Setup (once, before the demo)

```bash
pip install -e ".[dev]"
release-orchestrator lab-init --dest ./demo
```

To run the Skill from inside the library repository, make `/library-release` available
there. Either open Claude Code in this repository and `cd` into the lab in the Bash
commands, or link the Skill directory into `~/.claude/skills/library-release`.
`lro.py` resolves the link and always runs the engine from this repository.

## Script

Open Claude Code in `demo/harri-release-lab-library` (the library repository).

| # | Say / type | What the audience sees |
|---|---|---|
| 1 | `/library-release #15` | No path, no config flag. `LRO context` finds the repo, branch `main`, HEAD, config (`discovered`), stable `v1.0.0` and RCs rc.22/rc.23. The offline lab has no remote, so it asks for **one** thing: `--repository owner/name`. |
| 2 | answer `zaidmoen/zaidlibrary` | Context complete (`explicit override`). |
| 3 | (automatic) `resolve-change --pr 15` | PR #15 → **FIN-211** (from the title and branch). Jira: blocked by **FIN-198**. Related PR #12 implements FIN-198. FIN-211 is in `v1.1.0-rc.23`; FIN-198 is in rc.22 and rc.23, **not released**. PR data labelled `fixture`. |
| 4 | (automatic) `release-graph --ticket FIN-211` | `FIN-211 requires FIN-198`: **CONFIRMED** from **jira + code**. `rc.23 contains rc.22`: **ancestry, not a dependency**. Overlaps (same file) listed as INFERRED heuristics. Missing prerequisite: **FIN-198** → BLOCKED. |
| 5 | Q1 (release-candidate) | Releases listed in the question. "Only FIN-211" is **blocked**, with evidence. Pick "Include prerequisites: FIN-198, FIN-211". |
| 6 | Q2 | `All` (invoice-service AFFECTED, payment-service not affected). |
| 7 | Q3 | `prep`, `stage` (master shown as not selected). |
| 8 | (automatic) preview + `release-report` | `report.md`: repository, PR #15/FIN-211, mode, stable, RCs, graph, versions (`1.1.0-rc.24`), services 1.0.0 → 1.1.0-rc.24, envs prep/stage, evidence sources. **NEEDS REVIEW**: no validation evidence yet (honest, not READY). |
| 9 | "Give me the Slack summary" | `slack.txt` (copy-paste) and `slack.json` (Block Kit). Not sent. |
| 10 | `/library-release --mode reorder-releases --order v1.1.0-rc.23,v1.1.0-rc.22` | Q1: requested order **blocked**: FIN-211 needs FIN-198, which only rc.22 provides (jira + code). Ancestry shown as a note, not as the reason. Recommended: rc.22 → rc.23. |
| 11 | pick the blocked order anyway and ask for a report | Report **BLOCKED**, with "Why it is blocked" in Slack. Explain: the engine never lets you override it; the fix is to change the order, include FIN-198, or fix the Jira link if it's wrong. |
| 12 | `/library-release` (no argument) | Scenario D: 3 open PRs (#21, #19, #15) are offered. The Skill asks, and never picks. |
| 13 | `/library-release #19` | PR #19 has no ticket in its title or branch. Its body mentions FIN-215 (and an injection attempt, ignored). The Skill asks for the ticket. |

The same steps by hand (from `demo/harri-release-lab-library`):

```bash
release-orchestrator context
release-orchestrator --repository zaidmoen/zaidlibrary context
release-orchestrator resolve-change --pr 15
release-orchestrator resolve-change --pr 15 --ticket FIN-204
release-orchestrator resolve-change
release-orchestrator release-graph --ticket FIN-211
release-orchestrator release-graph --order v1.1.0-rc.23,v1.1.0-rc.22
release-orchestrator release-mode preview --mode release-candidate --ticket FIN-211 --choice with-prerequisites --services all --environments prep,stage --out p1.json
release-orchestrator --repository zaidmoen/zaidlibrary release-report --preview p1.json --pr 15 --out report-rc
release-orchestrator release-mode preview --mode reorder-releases --order v1.1.0-rc.23,v1.1.0-rc.22 --choice requested --services all --out p3.json
release-orchestrator release-report --preview p3.json --out report-reorder
```

## What the Skill accepts, discovers and produces

* **Accepts:** nothing, a PR (`#15`, `15`, PR URL, `--pr`), a Jira key, or both; optional
  `--mode`, `--candidate`, `--order`, `--repository owner/name`, `--config`.
* **Discovers automatically:** repository, remote, `owner/name`, branch, HEAD, default
  branch, config, stable release, pending and finalized RCs (Git tags, remote-only tags,
  GitHub Releases), PR ↔ ticket links, Jira status and links, related PRs, code-level
  dependencies, ancestry, affected services and their pins, environments and branches.
* **Produces:** Q1/Q2/Q3, an impact preview, a dependency graph (Mermaid), a dry-run
  report (Markdown/JSON) and a Slack summary (text and Block Kit).
* **Cannot determine:** live Jira data (it reads a snapshot), private GitHub data without a
  token, PRs that follow no convention, sheet contents unless exported, and whether a
  heuristic overlap is a real dependency (it stays INFERRED and needs review).
* **Needs your confirmation:** which PR when several match, the ticket for a PR without a
  key, PR/ticket conflicts, Q1/Q2/Q3, and every `--approve` step.
* **Protected by approval gates:** prepare, PR, push (allowlisted, never forced), publish
  (production publication always refused), consumer updates, environment promotion and
  rollback, and any Slack send (no send command exists in the Skill).
