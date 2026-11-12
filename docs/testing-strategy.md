# Testing strategy

The tests verify **outcomes in real systems** (Git history, remote refs, registry
contents, run state), not just that commands ran. Fakes are used only at the network edge
for GitHub REST (fake transport); everything else runs for real.

| Layer | Where | What is real |
|---|---|---|
| Unit | `tests/unit/` | pure logic: SemVer, version policy, approval policy, issue eligibility, manifest schema, guards, retry rules |
| Integration (Git) | `tests/integration/test_scenarios.py`, `tests/lab/` | temporary Git repositories, worktrees, cherry-picks, pushes to local bare remotes acting as GitHub |
| Integration (registry) | `tests/integration/test_pypi_registry.py` | a real pypiserver over HTTP with auth and server-side no-overwrite |
| Failure injection | `test_pr_reconcile.py`, `test_pypi_registry.py`, S12 | lost push response, lost/ambiguous publish response, locked tag ref, unreachable registry |
| Concurrency | `test_registry.py`, `test_pypi_registry.py`, `test_run_state_concurrency.py`, S10 | threads racing to publish; concurrent run-state writers; competing plans for one version |
| Security regression | S15 tests, `test_core.py` guard tests | malicious ticket text, unsafe commands, tampered manifest, non-allowlisted push, forced refspecs, token redaction |
| End-to-end (CLI) | `examples/lab/run_lab.py` | S01–S15 through the real CLI, with evidence recorded |
| Live (read-only / approved writes) | `docs/live-github-validation.md` | GitHub refs, PR refs, PR status, checks |

## Required regression scenarios → tests

| Scenario | Test |
|---|---|
| FIN-204 independent release | `test_s02_independent_out_of_order_release_excludes_a` |
| FIN-211 blocked dependency (code and tracker) | `test_s03_dependent_release_blocked_with_prerequisite` |
| Missing prerequisite / unresolved blocker | S03, `test_unresolved_blocker_blocks_fin_211` |
| Dependency cycle (code graph / issue links) | `test_graph_closure_and_cycles`, `test_circular_blockers_are_detected_once` |
| Divergent Git history | `test_release_pr_targets_integration_branch_at_release_base`, S10 |
| Incorrect provenance / manifest tamper | `test_verify_detects_extra_commit`, `test_s15_tampered_manifest_rejected` |
| Duplicate request | `test_s09_idempotent_requests`, `test_scenario_e_duplicate_execution` |
| Existing version / conflicting digest | `test_c_conflicting_digest_is_refused_by_adapter_and_server`, `test_registry_refuses_overwrite` |
| Lost push response | `test_push_whose_response_was_lost_is_not_pushed_twice` |
| Lost publish response | `test_d_lost_response_is_reconciled_without_duplicate_upload` |
| PR duplication / ambiguity / head change / missing | `test_pr_reconcile.py` |
| Stale approval / failed CI / draft / change request | `test_github_status.py`, `test_unverified_approval_blocks_publication` |
| Unauthorized publishing | `test_production_registry_without_policy_rejects_publish`, `test_operator_approve_flag_is_still_required` |
| Partial service update | `test_s11_breaking_api_major_and_consumer_protection` |
| Interrupted operation / restart | `test_s12_recovery_after_partial_publish_failure`, `test_restart_resumes_from_durable_state` |
| Concurrent operations | `test_concurrent_writers_with_reload_retry_lose_nothing`, `test_g_concurrent_publication_has_one_winner` |
| Credential failure | `test_f_authentication_failure_fails_safely`, `test_reader_does_not_retry_access_errors_and_never_leaks_token` |
| External service unavailable | `test_unreachable_registry_is_retryable_not_success`, `test_reader_gives_up_after_bounded_attempts` |
| Repeated RC promotion | **not covered**: RC finalization isn't implemented (see the assessment) |

## Commands

```bash
pip install -e ".[dev]"     # includes pypiserver for the registry tests
ruff check src tests .claude examples && ruff format --check src tests .claude examples
mypy --strict src/release_orchestrator
pytest -q
python examples/lab/run_lab.py
```
