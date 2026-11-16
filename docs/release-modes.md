# Release modes: release-candidate, release, reorder-releases

Design: [ADR-0014](adr/0014-interactive-release-modes.md). Every step below is
**read-only** until a command with `--approve` is run.

## The interactive flow (what the Skill does)

| Step | Command | User interaction |
|---|---|---|
| 0. Discovery | `release-orchestrator release-mode discover` | none: summary of the stable release, pending RCs, tickets, services, environments and uncertainties |
| Q1. Dependency check | `release-orchestrator release-mode options --mode <mode> [...]` | **single choice** among evidence-backed options (blocked ones shown with reasons) |
| Q2. Where to apply | (preview `--services a,b` or `all`) | **multi select** of discovered services |
| Q3. Environments | (preview `--environments prep,stage` or omit) | **optional multi select**; omitted = no environment operation |
| Preview | `release-orchestrator release-mode preview ... --out preview.json` | read the table, blockers and commands |
| Approval gate | `release-orchestrator release-mode check-preview --preview preview.json`, then each listed command | explicit "yes" before every `--approve` |

Mode-specific options: `--ticket` (release-candidate scope), `--candidate`
(release), `--order rcA,rcB` (reorder-releases).

Statuses: **READY** (all checks plus validation evidence), **NEEDS VALIDATION**
(checks pass, evidence still missing), **BLOCKED**, **NOT SELECTED**.

## Copy-paste demo (fictional lab, fully local)

```bash
pip install -e ".[dev]"
release-orchestrator lab-init --dest ./.modes-demo
cd .modes-demo
release-orchestrator release-mode discover
```

**Scenario 1: release candidate and its impact on consumers**

```bash
release-orchestrator release-mode options --mode release-candidate --ticket FIN-211
release-orchestrator release-mode preview --mode release-candidate --ticket FIN-211 --choice with-prerequisites --services all --environments prep --out s1.json
```

**Scenario 2: release a candidate after checking dependencies and approvals**

```bash
release-orchestrator release-mode options --mode release --candidate v1.1.0-rc.22
release-orchestrator release-mode preview --mode release --candidate v1.1.0-rc.22 --choice release:v1.1.0-rc.22 --services payment-service --environments prep,stage --out s2.json
release-orchestrator release-mode check-preview --preview s2.json
release-orchestrator plan --ticket FIN-198 --ticket FIN-204 --base v1.0.0 --source v1.1.0-rc.22 --out plan-1.json
release-orchestrator prepare --plan plan-1.json --approve
release-orchestrator validate --plan plan-1.json
release-orchestrator verify-lineage --plan plan-1.json --candidate v1.1.0-rc.22
```

**Scenario 3: release rc.23 before rc.22, if dependencies allow**

```bash
release-orchestrator release-mode options --mode reorder-releases --order v1.1.0-rc.23,v1.1.0-rc.22
release-orchestrator release-mode preview --mode reorder-releases --order v1.1.0-rc.23,v1.1.0-rc.22 --choice requested --out s3.json
release-orchestrator release-mode preview --mode reorder-releases --order v1.1.0-rc.23,v1.1.0-rc.22 --choice dependency-order --services all --out s3-safe.json
```

## Observed results (local run on the lab, 2026-11-16)

**Discovery:**
* Stable `v1.0.0`.
* `v1.1.0-rc.22` = FIN-198 and FIN-204.
* `v1.1.0-rc.23` contains rc.22 (Git ancestry); its own change is FIN-211.
* Services pin `zaidlibrary==1.0.0`.
* Environments prep, stage and master map to the same-named branches.
* Uncertainty reported: no `origin` remote in the local lab, so GitHub state wasn't checked.

**Scenario 1:** Q1 recommended `with-prerequisites`; `requested-only` is BLOCKED
(FIN-211 imports `zaidlibrary.currency.convert`, which only exists after FIN-198, and the
tracker says FIN-211 is blocked by FIN-198). Preview, readiness **NEEDS VALIDATION**:

| Service | Current Version | Target Version | Environment | Status | Notes |
|---|---|---|---|---|---|
| zaidlibrary | 1.0.0 | 1.1.0-rc.24 | prep | NEEDS VALIDATION | library release |
| payment-service | 1.0.0 | 1.0.0 | prep | READY | not affected; no change needed |
| invoice-service | 1.0.0 | 1.1.0-rc.24 | prep | NEEDS VALIDATION | consumer tests run against the published artifact at update time |

**Scenario 2:** the preview targets 1.1.0 with readiness NEEDS VALIDATION; invoice-service
is `NOT SELECTED` (not affected). The first approved commands were run:
* plan `87df67c15627f0ad` was `PLAN_READY`;
* `prepare` created the branch;
* `validate` **PASSED**;
* `verify-lineage` returned **ok**: the only difference from `v1.1.0-rc.22` is
  `src/zaidlibrary/__init__.py`.

**Scenario 3:** Q1 recommended `dependency-order`.

| Option | Result |
|---|---|
| `requested` (rc.23 → rc.22) | **BLOCKED**; step 2 `SKIPPED` |
| `dependency-order` (rc.22 → rc.23) | valid: 1.1.0 then 1.2.0 |
| `supersede` (rc.23 only) | valid: 1.1.0 |

The requested-order preview exits 3, with unresolved dependencies FIN-211 → FIN-198 from
both code analysis and the tracker.

## Not verified / limitations

* Live GitHub approvals and checks for the lab repos need a token (see
  live-github-validation.md). Without one, approval stays NEEDS VALIDATION and never READY.
* Live Jira isn't connected (ADR-0013). Ticket links come from the snapshot.
* Environment promotion is a ledger record; branches named in config aren't moved by
  the orchestrator.
* Cross-repository execution isn't atomic. Steps are idempotent and resumable instead.
