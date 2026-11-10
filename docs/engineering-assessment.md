# Engineering assessment (updated 2026-11-10)

This assessment is against realistic release-management needs for a shared library with
many microservice consumers, out-of-order business releases, RC trains, and
prep → stage → master environments. Evidence: `docs/live-github-validation.md`,
`examples/lab/RESULTS.md`, and 126 automated tests.

## Capability classification

| Capability | Status | Evidence / gap |
|---|---|---|
| Commit → ticket mapping, ambiguity detection | VERIFIED LOCALLY | S15; unit tests |
| Out-of-order release (B without A), A provably absent | **VERIFIED LIVE** | L-S02: `release/1.1.0` on GitHub, `currency.py` absent, patch-ids equal |
| Code-dependency blocking (B needs A) | **VERIFIED LIVE** | L-S03: blocked, no remote write |
| Textual conflict detection | VERIFIED LOCALLY | S04 |
| Semantic dependency caught by validation | VERIFIED LOCALLY | S05 (tests are the only defence) |
| Release branch provenance and manifest integrity | **VERIFIED LIVE** | `verify`: all checks pass on the pushed branch |
| SemVer recommendation, collisions | VERIFIED LOCALLY | S10, unit tests |
| Release candidates, cumulative-RC subsets | VERIFIED LOCALLY | S06 |
| RC lifecycle (rc → final promotion of the same content, RC retirement) | PARTIALLY IMPLEMENTED | RCs can be planned and published; no "finalize rc.N as 1.1.0 from the same commit" step |
| Immutable publication | PARTIALLY IMPLEMENTED | Local registry only (digest-checked, `O_EXCL`). No production registry adapter |
| Tag publication on GitHub | **VERIFIED LIVE** | `v1.1.0` → `2c8e859e47`, never moved |
| Allowlisted, never-forced push | **VERIFIED LIVE** | Adds only; refs before and after diffed |
| Idempotent re-runs | **VERIFIED LIVE** | Full re-run: remote refs byte-identical, all steps `already_*` |
| Consumer targeting (only affected services) | **VERIFIED LIVE** | payment-service branch pushed, tests pass; invoice-service untouched |
| Consumer protection on breaking API | VERIFIED LOCALLY | S11 |
| Release PR topology (integration branch) | **VERIFIED LIVE** (branch) | `release-base/v1.0.0` pushed. The PR itself is blocked (below) |
| PR discovery and reconciliation (`reconcile-prs`) | **VERIFIED LIVE** (read-only) | Found PR #1 on `zaidmoen/library-release-orchestrator` with the correct head and URL; reported `not_created` on both lab repos, leaving remotes unchanged. Drift, duplicate and missing cases VERIFIED LOCALLY (ADR-0010) |
| Network retries (transient only, lost-push safe) | VERIFIED LOCALLY | `test_pr_reconcile.py` |
| Draft PRs on the lab repos | **BLOCKED** | Private repos on GitHub Free have no draft PRs; the connector has no access to them (404). Repo visibility stays unchanged by instruction |
| PR status read-back via MCP | BLOCKED | Same cause |
| Python GitHub REST adapter | PARTIALLY IMPLEMENTED (MOCKED) | No token available |
| Environment promotion (prep/stage/master) | VERIFIED LOCALLY | S13 |
| Rollback (roll-forward to the previous artifact) | VERIFIED LOCALLY | S14 |
| Authenticated approvals | NOT IMPLEMENTED | Approver names are self-declared |
| Jira integration | NOT IMPLEMENTED | YAML fixture adapter only |
| CI-executed validation | NOT IMPLEMENTED | Validation runs on the operator's machine |
| Lockfile ecosystems (Poetry, uv, pip-tools) | NOT IMPLEMENTED | `requirements.txt` `==` pins only |

## Prioritized plan for the next iteration

1. **Unblock live PRs and close the evidence loop.** Make the sandbox repos public or grant
   the GitHub app access. Create both draft PRs, then `record-pr`, PR status read-back and an
   idempotency re-run. Add a CI workflow to the lab repos so validation also runs on GitHub.
2. **Production package registry adapter** (CodeArtifact, Artifactory or private PyPI) with
   create-if-absent semantics. Move publication into CI after the release PR is approved,
   and drop the local `publish` step for production.
3. **RC lifecycle.** Add `finalize --from 1.1.0-rc.24`, which publishes the final version from
   the *same* release commit and verifies the digest lineage. Add RC retirement and a rule
   that master only accepts finals finalized from a stage-promoted RC.
4. **Authenticated approvals and promotion.** Take approvals from GitHub PR reviews or
   environment protection rules instead of `--approved-by`. Store the environment ledger in
   a reviewed config repo or a database. Hand ledger records to the deploy system.
5. **Jira REST adapter** with a read-only service account: ticket status gates (only
   "Ready for release"), consumer fields, and the release fix-version written back only
   with approval.
6. **Consumer coordination at scale.** Discover consumers from a service catalog instead of
   config, use symbol-level impact, support lockfile ecosystems, run consumer CI on the
   PR, and track adoption (which services pin which version).
7. **Rollback across consumers.** Generate per-service revert PRs from `rollback-plan`.
   Optionally add a "yank" marker in the registry (the version stays immutable but is
   flagged as not for new adoption).

## Bottom line

The decision engine is reliable in observable behaviour. On real GitHub it released B
without A, blocked a dependent change, verified provenance, refused to duplicate anything
on re-run, and touched only the affected consumer. Production adoption is held back by
integrations and governance (registry, authenticated approvals, Jira, CI validation), not
by the core release logic.


## Enterprise gap analysis (2026-11-10)

Each row gives the weakness observed and the *smallest* maintainable fix.
✅ = done in this iteration.

| # | Area | Current state | Weakness | Smallest solution |
|---|---|---|---|---|
| 1 | Dependency consistency | Static Python analysis, apply probe, tests as backstop | Behavioural dependencies only caught by tests; Python only | Make the consumer test matrix mandatory in CI for `INDEPENDENT_WITH_RISKS`; add a language adapter interface only when a second language is needed |
| 2 | Version correctness | Content-based SemVer, collision checks, RC numbering | The registry view is local; tags and registry can disagree across machines | Read versions from the production registry adapter (P1) |
| 3 | Atomicity / partial failure | Branch created only after success; publish → tag recovery | Multi-repo steps (library + N consumers) aren't atomic | Keep per-step idempotency plus `reconcile-prs`; extend reconciliation to tags and consumer branches |
| 4 | Idempotency | Live-verified (refs byte-identical) | PR existence relied on `record-pr` | ✅ `reconcile-prs` discovers and verifies PRs (ADR-0010) |
| 5 | Concurrent requests | Same-version collisions blocked (S10); local `O_EXCL` | No cross-machine lock on run state | Rely on registry create-if-absent (P1) and protected `release/*` refs; don't add a distributed lock |
| 6 | PR lifecycle | Creation is external (MCP or human) | Couldn't see externally created PRs; no drift detection; no status | ✅ Discovery, recording, drift and duplicate detection. **Remaining:** open/draft/merged status needs API access |
| 7 | Artifact immutability | Digest-checked local registry | Not a production registry | P1: registry adapter with create-if-absent and sha256 verification on fetch |
| 8 | Registry publishing | Local step `publish --approve` | Should run in CI after approval | Move to a CI job triggered by the approved release PR; keep the CLI for the lab |
| 9 | Approval integrity | `--approve` plus self-declared names | Not authenticated | P3: take approvals from GitHub PR reviews (`get_reviews`) or environment protection rules |
| 10 | Jira traceability | YAML fixture | No live ticket state | P4: read-only Jira adapter; gate on status "Ready for release" |
| 11 | Multi-service coordination | Module-level impact; `==` pins only | Lockfiles and service catalog missing | Lockfile updaters (uv/Poetry) and catalog-driven discovery |
| 12 | Rollback | Environment roll-forward plan | No per-consumer revert PRs | Generate consumer re-pin PRs from `rollback-plan` |
| 13 | Auditability | Run state, reports, provenance, ledger | Local files only | Ship reports as release-PR artifacts or to a reviewed repo |
| 14 | Security boundaries | Allowlist, guarded Git, approvals, untrusted ticket text | Validation runs with operator privileges | Run validation in CI or containers |
| 15 | Observability | Structured logs (`--log-json`), JSON envelopes | No metrics or alerting | Emit one JSON event per state transition; let the platform's log pipeline handle metrics |

### Next three recommended engineering tasks

1. **Production registry adapter (P1)** with create-if-absent, digest verification on fetch,
   and contract tests against a local PyPI-compatible server (for example `pypiserver` in
   CI). It unblocks correct cross-machine versioning and CI-based publishing.
2. **GitHub-backed approvals and PR status (P3 + rest of P0).** Read reviews and draft/merged
   state through an authenticated GitHub API (MCP with repo access, or a fine-grained token
   scoped to the release repos). Gate `publish` and `promote` on real approvals.
3. **RC finalization (P2).** `finalize --from <rc>` publishes the final version from the
   same release commit, checks the RC was promoted to stage, and records the lineage
   rc → final in the ledger.
