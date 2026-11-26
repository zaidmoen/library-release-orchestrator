# Capability audit (start of `feat/real-world-release-lab`)

This audit was done against the code on `main` at `581deca`, not against the docs.
Verification levels used throughout the project:

| Level | Meaning |
|---|---|
| **LIVE GITHUB VERIFIED** | Exercised against real GitHub repositories, with evidence recorded in `docs/evidence/` |
| **LOCAL INTEGRATION** | Exercised end to end against real Git repositories on disk, in automated tests |
| **MOCKED** | Only exercised against a fake (for example a fake HTTP transport) |
| **NOT IMPLEMENTED** | Designed or documented only |

## What actually works (before this branch)

| Capability | Level | Evidence |
|---|---|---|
| Commit → change mapping, multi-commit changes, ambiguous and unmapped commits | LOCAL INTEGRATION | `tests/unit/test_core.py`, scenario tests |
| Python symbol/module dependency analysis, sandbox cherry-pick probe | LOCAL INTEGRATION | Scenarios B and C |
| Deterministic plan, manifest schema, tamper detection, branch verification | LOCAL INTEGRATION | `test_plan_file_roundtrip_and_tamper_detection`, `test_verify_detects_extra_commit` |
| Isolated release branch (detached worktree), dry-run with no mutation | LOCAL INTEGRATION | `test_dry_run_is_non_mutating`, `test_prepare_does_not_touch_user_worktree` |
| Allowlisted validation, failure blocks | LOCAL INTEGRATION | Scenario F |
| Immutable local registry, single-winner concurrent publish | LOCAL INTEGRATION | `test_registry.py` |
| Consumer targeting and `==` pin updates validated against the artifact | LOCAL INTEGRATION | Scenario D |
| Local PR records | LOCAL INTEGRATION | Scenarios D and E |
| GitHub REST PR adapter (draft only) | MOCKED | Fake transport only; no token was ever available |
| Claude Skill → CLI | LOCAL INTEGRATION | `test_skill.py` |

## Genuine gaps found

1. **No remote writes at all.** The Git guard forbids every `push`, so nothing can reach
   GitHub. Live PRs are therefore impossible end to end.
2. **No path to live PRs without a token.** The REST adapter needs `GITHUB_TOKEN`. The
   environment has none, but the GitHub MCP server is available and gates each PR behind
   a user-submitted form.
3. **No release candidates.** `recommend_version` only produces final versions. There is
   no RC numbering and no handling of cumulative RC histories.
4. **No environment model.** There is no concept of prep, stage or master, no promotion
   rules, no approval quorum and no rollback beyond a `rollback_version` field.
5. **Breaking-API detection is untested end to end.** It is wired into the analyzer but
   only the bump policy has a test. Consumer breakage on a major release isn't exercised.
6. **Recovery is tested for only one failure point** (a stale branch). Publish-then-tag
   failure and partial consumer failure aren't exercised.
7. **Concurrency** is tested at registry level only, not as two competing plans.
8. **Demo histories are minimal**: one or two commits per scenario, not a realistic
   trunk with several pending tickets.
9. **Remote URLs aren't constrained.** Nothing restricts *where* a future push could go.

## Plan for this branch

| Gap | Response |
|---|---|
| 1, 9 | Approved, non-force `push` step limited to an explicit sandbox allowlist (ADR-0008) |
| 2 | An `external` PR provider: the engine writes a PR request, Claude creates it through the GitHub MCP server (user-approved form), and the result is recorded back (ADR-0008) |
| 3 | RC versioning (`--prerelease rc`), next RC number per target version, cumulative-RC scenario |
| 4 | Environment ledger with ordered promotion, approval quorum, prerelease policy and rollback plans (ADR-0007) |
| 5, 6, 7 | New scenarios S10, S11 and S12 with failure injection |
| 8 | A realistic lab generator (`release_orchestrator.lab`): one trunk, seven tickets, RC tags, two consumers |
