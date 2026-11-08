# Engineering assessment (2026-11-08)

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
| Draft PRs on GitHub | **BLOCKED** | Human step pending: private repos on GitHub Free have no draft PRs; the connector has no repo access |
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
