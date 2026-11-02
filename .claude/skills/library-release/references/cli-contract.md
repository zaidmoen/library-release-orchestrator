# CLI contract (summary)

Full reference: `docs/cli.md` in the repository.

Every call prints one JSON envelope:

```json
{"ok": true, "command": "plan", "exit_code": 0, "result": {...}, "error": null}
```

On failure, `error` is `{"code", "message", "details", "reason"?}`. Key `reason` values:
`PLAN_BLOCKED`, `GIT_CONFLICT`, `MANIFEST_MISMATCH`, `BRANCH_EXISTS_MISMATCH`,
`VALIDATION_FAILED`, `NOT_VALIDATED`, `VERSION_COLLISION`, `VERSION_NOT_PUBLISHED`,
`IMMUTABLE_VERSION`, `BRANCH_NOT_PUSHED`, `NOTHING_TO_RELEASE`.

| Exit | Meaning | Skill action |
|---|---|---|
| 0 | ok | continue |
| 1 | internal error | report verbatim, stop |
| 2 | invalid input or config | show the message, ask for a fix |
| 3 | blocked by a safety rule | explain blockers and remediation, stop |
| 4 | approval required | ask the user, re-run with `--approve` only on a yes |
| 5 | ambiguous or not found | ask the user to clarify |
| 6 | Git or integration operation failed | report; retrying is safe (idempotent) |

Useful result fields:

* `plan`: `result.plan.status`, `.analysis.verdict`, `.analysis.findings[]`,
  `.release.proposed_version`, `.included_changes[]`, `.excluded_changes[]`,
  `.consumers[]`, `.blockers[]`, `.remediation[]`
* `prepare`: `status` (`prepared` | `already_prepared` | `dry_run_ok`), `branch`, `provenance[]`
* `validate`: `status`, `results[].output_tail`
* `update-consumers`: `updated[]` (with validation and PR), `not_updated[]`
