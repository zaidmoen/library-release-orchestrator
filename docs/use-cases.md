# Use cases

Status legend: **Implemented** = working code plus automated test.
**Partial** = core behaviour implemented, some aspects planned.
**Designed** = extension point and documented approach, not automated yet.

| # | Use case | Status | How it is handled | Evidence |
|---|---|---|---|---|
| 1 | Normal library release | Implemented | `plan` → `prepare` → `verify` → `validate` → `pr` → `publish` with all pending changes requested. | `tests/integration/test_scenarios.py::test_normal_release_all_changes` |
| 2 | Out-of-order release | Implemented | Request B only. B is applied with `cherry-pick -x` onto the last released base and A is excluded. Release 2 uses the 1.1.0 release commit as `--base`. | `test_scenario_a_independent_out_of_order`, `test_second_release_after_out_of_order` |
| 3 | Immutable published versions | Implemented | `LocalRegistry` uses `O_EXCL`. Same digest is idempotent; different digest raises `ImmutableVersionError`. | `tests/unit/test_registry.py` |
| 4 | Version collision | Implemented | Candidate checked against registry **and** tags. A collision gives a `VERSION_COLLISION` blocker, never a silent renumber. | `test_scenarios.py::test_collision_with_tag_blocks` (a concurrent tag appears between plan and publish) |
| 5 | Concurrent release operations | Partial | Publication is the atomic serialisation point (ADR-0005). Two threads publishing the same version: exactly one wins. Cross-machine safety depends on the production registry. | `test_registry.py::test_concurrent_publish_single_winner` |
| 6 | Release branch isolation | Implemented | Detached temporary worktree; branch created only after full success; user checkout untouched. | `test_prepare_does_not_touch_user_worktree` |
| 7 | Code-level dependencies | Implemented (Python) | AST symbol and module analysis: B references a symbol or module introduced by excluded A, so `DEPENDENT` and blocked, with remediation. | `test_scenario_b_dependent_blocked` |
| 8 | Multiple commits per change | Implemented | Commits grouped by change key, applied in original topological order. | `tests/unit/test_core.py::test_groups_multiple_commits` |
| 9 | Cherry-pick conflicts | Implemented | Sandbox probe detects the conflict and lists conflicting files. Approved prepare aborts cleanly and leaves no branch. | `test_scenario_c_conflict_*` |
| 10 | Missing or ambiguous Jira mappings | Implemented | Unknown key → `NOT_FOUND` (exit 5). Commit naming two keys → ambiguous finding. Key with no commits → `NO_COMMITS`. | `test_core.py::test_ticket_validation`, `test_core.py::test_ambiguous_commit` |
| 11 | Breaking API changes | Partial (Python) | Removed public top-level symbols are detected and force a major bump, which is listed as a compatibility risk for consumers. Detection is wired into the analyzer, but only the version policy has a dedicated test so far. | `test_versioning.py::test_removed_public_symbol_forces_major` |
| 12 | Failing automated tests | Implemented | Validation failure → `BLOCKED`, PR refused, output tail recorded. | `test_scenario_f_failed_validation` |
| 13 | Production rollback planning | Designed | Releases are immutable, so rollback = consumers re-pin the previous version (the report lists `rollback_version` per consumer), or a revert release is cut as a new version. No deployment is performed. | Report field `rollback` |
| 14 | Release manifest mismatch | Implemented | `verify` recomputes ancestry, cherry-pick trailers and `patch-id`s. Extra, missing or altered commits are reported. A tampered manifest fails schema/ID checks. | `test_verify_detects_extra_commit`, `test_plan_file_roundtrip_and_tamper_detection`, `test_manifest_schema_rejects_garbage` |
| 15 | Read-only dry-run | Implemented | `analyze`, `plan` and `prepare --dry-run` use sandbox clones. Refs, index, worktree list and working tree are snapshotted and compared. | `test_dry_run_is_non_mutating` |
| 16 | Permissions and human approval | Implemented | Write commands exit `4` without `--approve`; the Skill asks the user. Permission to push or open live PRs comes from the user's own credentials. | `test_adapters_and_cli.py::test_write_commands_require_approval`, `test_approval_required_for_writes` |
| 17 | Microservice dependency updates | Implemented | Consumers discovered from config, filtered by ticket metadata and import analysis, pin rewritten on a branch, consumer tests run against the published artifact. | `test_scenario_d_consumer_updates` |
| 18 | Jira ticket input | Implemented (fixture) | `FixtureIssueTracker`; the Jira REST adapter is planned. | `test_core.py::test_ticket_validation`, `test_untrusted_consumer_names_filtered` |
| 19 | Natural-language input | Partial | Engine extracts keys and configured aliases ("Change B") deterministically. Claude asks for clarification when the result is ambiguous. | `test_core.py::test_natural_language_alias`, `test_skill.py` |
| 20 | Idempotent execution | Implemented | Plan ID content hash; existing branch, artifact or PR recognised. | `test_scenario_e_duplicate_execution` |
| 21 | Partial workflow failure and recovery | Implemented | Run state persisted per step. Re-running resumes; a failed prepare leaves no residue. | `test_recovery_after_failed_prepare` |
| 22 | End-to-end traceability | Implemented | Report links request → ticket → source SHAs → result SHAs (`cherry picked from`) → version → artifact digest → consumer branches and PRs. | `test_report_traceability` |

## Scenario mapping (POC demo)

| Demo scenario | Use cases |
|---|---|
| A: Independent changes | 2, 6, 15, 22 |
| B: Dependent changes | 7 |
| C: Conflicting changes | 9, 21 |
| D: Consumer updates | 17, 3 |
| E: Duplicate execution | 20 |
| F: Failed validation | 12 |
