# Operations runbook

Every command prints a JSON envelope with `--json`. `error.reason` tells you which section
below applies. Add `--log-json` to get structured logs on stderr (`state_transition`
events carry `plan_id`, `from_state`, `to_state`, `version`; retries carry `attempt` and
`delay`).

## Normal release

```bash
release-orchestrator plan --ticket FIN-204 --out plan.json        # read-only
release-orchestrator prepare --plan plan.json --dry-run
release-orchestrator prepare --plan plan.json --approve
release-orchestrator validate --plan plan.json
release-orchestrator pr --plan plan.json --approve                # writes a PR request
release-orchestrator push --plan plan.json --approve              # allowlisted, never forced
release-orchestrator reconcile-prs --plan plan.json               # create a PR only if not_created
release-orchestrator pr-status --plan plan.json                   # approval decision (needs policy)
release-orchestrator publish --plan plan.json --approve
release-orchestrator update-consumers --plan plan.json --approve
release-orchestrator push --plan plan.json --approve
release-orchestrator status --plan plan.json
```

## Troubleshooting by `reason`

| Reason / code | Meaning | What to do |
|---|---|---|
| `MISSING_PREREQUISITE`, `DIRECT_CODE_DEPENDENCY`, `UNRESOLVED_BLOCKER` | the change needs another change | choose a remediation from the plan (include prerequisite, split, flag); never force |
| `GIT_CONFLICT` | doesn't apply on the base | the author makes a backport commit; re-plan |
| `ISSUE_DATA_STALE` / `ISSUE_INACCESSIBLE` | issue snapshot is old or partial | refresh the snapshot, then re-plan |
| `VALIDATION_FAILED` | tests failed on the release branch | read `output_tail`; fix on trunk; re-plan |
| `APPROVAL_NOT_VERIFIED` | review/check policy not satisfied | read `details.reasons` (draft, stale approval, failing check); get a review on the **current** head |
| `PRODUCTION_PUBLISH_UNAUTHORIZED` | production registry without a policy | configure `approval_policy`; never bypass |
| `IMMUTABLE_VERSION` / `VERSION_COLLISION` | the version exists with other content | investigate the other release; re-plan to the next version; never overwrite |
| `REGISTRY_AUTH_FAILED` / `GITHUB_ACCESS_DENIED` | credentials missing, wrong or too weak | fix the env var or token scope; nothing was written |
| `INTEGRATION_ERROR` with `retryable: true` | network or server outage after retries | re-run the same command later (steps are idempotent) |
| `recorded_head_moved` / `recorded_missing` / `ambiguous` (reconcile) | PR drift or duplicates | inspect on GitHub; close duplicates manually; `record-pr` the right one |
| `CONCURRENT_MODIFICATION` | another process changed the run | re-run the command; if `locked` persists and no process is running, remove `<state_dir>/runs/<plan_id>.lock` |
| `BRANCH_EXISTS_MISMATCH` / `INTEGRATION_BRANCH_MISMATCH` | a ref exists with other content | the engine never moves it; investigate and rename it manually if it's stale |

## Recovery semantics

| Situation | Safe action | Never |
|---|---|---|
| Plan not executed | discard `plan.json` (nothing was written) | — |
| Failed prepare | re-run (no branch is created on failure) | `reset --hard` someone's branch |
| Publish stored, tag failed (`FAILED`) | re-run `publish`: reuses the artifact, creates the tag | delete the artifact |
| Push response lost | re-run `push`: compares remote refs first | force-push |
| Consumer validation failed | no branch/PR is created; fix compatibility, re-run | ship the bump anyway |
| Unmerged consumer PR must be withdrawn | close the PR on GitHub (human) | delete branches automatically |
| Bad release already published | publish a fixed **new** version; consumers re-pin; `rollback-plan`/`rollback` for environments | overwrite or delete the published version or tag |
| Deployment rollback | out of scope; hand the ledger's previous version to the deploy system | — |
