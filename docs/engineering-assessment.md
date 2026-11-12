# Engineering assessment (updated 2026-11-10)

Evidence sources: `docs/live-github-validation.md` (live GitHub), the automated suite
(`docs/testing-strategy.md`), `examples/lab/RESULTS.md` (CLI end to end), ADR-0001…0013.

## Architecture in one paragraph

Claude (the `/library-release` Skill) interprets requests, calls the CLI, relays evidence
and asks humans for consent. It never decides correctness. The deterministic engine
(`release_orchestrator`) owns dependency analysis, planning, versioning, the run state
machine, policy checks and audit records. Adapters isolate Git remotes, GitHub REST, the
package index and the issue-tracker snapshot. Each one verifies authoritative external
state before and after writes. Durable state lives in `<state_dir>`: run files with
compare-and-swap revisions, reports, PR requests and the environment ledger.

## Capability classification

| Capability | Status | Evidence |
|---|---|---|
| Dependency-aware selection (FIN-204 alone, FIN-211 blocked) | **VERIFIED LIVE** | lab repos on GitHub; refs and SHAs in live validation §2 |
| Provenance, patch-ids, manifest integrity, tag | **VERIFIED LIVE** | `verify` on the pushed branch |
| Idempotent re-runs (remote refs unchanged) | **VERIFIED LIVE** | live validation §4 |
| Allowlisted, never-forced push; lost-response safe | VERIFIED LIVE (pushes) / VERIFIED LOCALLY (failure injection) | live §2; `test_push_whose_response_was_lost_is_not_pushed_twice` |
| Release PR topology (integration branch) | VERIFIED LIVE (branch pushed) | ADR-0009 |
| PR discovery and reconciliation via refs | **VERIFIED LIVE** (read-only) | PR #1 and #2 found; lab `not_created` |
| Authoritative PR status, reviews, checks | **VERIFIED LIVE** (read-only, public repo) | `github-pr-status` on PR #2 |
| Approval policy (current-head approvals, stale, change requests, checks) | VERIFIED LOCALLY + live read | `test_github_status.py`, `test_publish_authorization.py`; PR #2 correctly "not approved" |
| Publication gated on verified approval (current-head, verified reviewer permission, green checks; fail closed); production registry always refused | VERIFIED LOCALLY | `test_publish_authorization.py`, `test_github_status.py` |
| Package index publication (PEP 503), immutability, reconciliation, concurrency | VERIFIED LOCALLY against a real pypiserver | `test_pypi_registry.py` |
| Production registry | NOT EXERCISED (by design) | needs credentials and explicit authorization |
| Reproducible wheels | VERIFIED LOCALLY | identical sha256 across builds |
| Issue eligibility (status, blockers, cycles, stale, inaccessible) | VERIFIED LOCALLY | `test_issue_eligibility.py` |
| Live Jira | **BLOCKED** | connected site is the company's production Jira; no sandbox project |
| Lab PRs on private repos | **BLOCKED** | GitHub Free has no drafts on private repos; connector 404; repos stay private |
| RC planning and cumulative-RC subsets | VERIFIED LOCALLY | S06 |
| RC → final promotion (`finalize`) | **NOT IMPLEMENTED** | design in ADR-0012 consequences |
| Environment ledger, promotion, rollback plan | VERIFIED LOCALLY | S13, S14 (approver names self-declared) |
| Multi-service coordination, partial failure, no unneeded changes | VERIFIED LIVE (payment yes, invoice untouched) / LOCALLY (breaking API) | live §2, S11 |
| Run-state concurrency (CAS revisions) | VERIFIED LOCALLY | `test_run_state_concurrency.py` |
| Structured transition logs | VERIFIED LOCALLY | `--log-json` emits `state_transition` events |
| Python GitHub PR *creation* adapter | MOCKED | no token |

## Definition of Done (brief §24)

| Item | Status |
|---|---|
| Existing release engine remains correct | ✅ full suite green (see PR #2 description for the exact run) |
| Dependency resolution deterministic and validated | ✅ |
| Release manifests traceable | ✅ |
| GitHub PR reconciliation works | ✅ (live read-only) |
| Authenticated PR lifecycle information reliable | ⚠️ public repo verified live; private lab repos blocked on token/access |
| Registry publication safe and verifiable | ✅ against a real isolated index; ⚠️ no production registry |
| RC handling and promotion obey invariants | ⚠️ RC planning yes; final promotion not implemented |
| Approval checks authoritative | ✅ for publication (GitHub reviews on the head); ⚠️ environment promotion still uses self-declared names |
| Jira integrated where access permits | ⚠️ engine side done; live **blocked** |
| Multiple services coordinated safely | ✅ |
| Interrupted operations reconciled | ✅ (publish→tag, push, PR, registry) |
| Recovery semantics defined and tested | ✅ runbook; ⚠️ no automated `recovery-plan` command |
| Concurrent operations don't corrupt state | ✅ registry CAS, run-state CAS, version collisions |
| Security boundaries enforced | ✅ |
| Skill invokes only real functionality | ✅ intent map lists only existing commands |
| Regression scenarios pass | ✅ |
| Documentation describes the actual system | ✅ |
| Every unverified external dependency identified | ✅ (this table) |

## Production readiness

**Not production-ready.** Classification: *validated engine, partial integrations*.
Blocking enterprise deployment:

1. **Credentials and live access**: a fine-grained `GITHUB_TOKEN` for private release
   repos; a production package index account; a sandbox Jira project.
2. **RC → final promotion** isn't implemented.
3. **Shared state**: run files and the ledger are single-host (CAS plus a lock file). A
   multi-runner deployment needs a database with row versions.
4. **Validation sandboxing**: tests run with operator privileges. Move them to CI runners.
5. **Environment approvals** still use self-declared names; reuse the ADR-0011 verifier
   (deployment environments' required reviewers).

## Next actions (priority order)

1. Provision a fine-grained token for the lab repos, then verify `pr-status` and the
   publish gate live on a private repo. Decide whether lab repos can use regular (non-draft)
   PRs. That needs explicit authorization.
2. Implement `finalize --from <rc>`: promote the approved RC's release commit plus a
   version-only commit; verify lineage and the digest relationship; refuse when the RC
   wasn't validated or approved.
3. Point the registry adapter at a staging index (Artifactory/CodeArtifact) under a
   non-production account, and repeat the A–G matrix live.
4. Snapshot refresh job for Jira (read-only service account), then live eligibility on a
   sandbox project.
5. Move run state and the ledger to a shared store with optimistic locking. Add a
   `recovery-plan` command derived from the runbook table.
