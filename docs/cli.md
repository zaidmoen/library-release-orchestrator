# CLI reference

```
release-orchestrator [--config FILE] [--json] [-v] [--log-json] <command> [options]
```

* `--config`: trusted config. Default is `$LRO_CONFIG`, then `./release-orchestrator.yaml`.
* `--json`: print exactly one JSON envelope on stdout. Logs always go to stderr.

## Commands

| Command | Writes | Options | Purpose |
|---|---|---|---|
| `analyze` | nothing | one of `--ticket K` (repeatable), `--request TEXT`, `--commit SHA` (repeatable); `--base REF`, `--source REF` | Dependency analysis only. Exit 3 if there are blocking findings. |
| `plan` | run state; `--out` file plus `.md` | same as `analyze`, plus `--out FILE` | Deterministic release plan (manifest). Exit 3 if `BLOCKED`. |
| `validate-manifest` | nothing | `--plan FILE` | Schema check, `plan_id` integrity, and comparison with the trusted config. |
| `prepare` | release branch | `--plan FILE`, `--approve`, `--dry-run` | Build the isolated release branch. `--dry-run` works in a throwaway clone. |
| `verify` | nothing | `--plan FILE` | Re-derive the branch content from Git and compare it with the plan. |
| `validate` | nothing (runs in a `git archive` export) | `--plan FILE` | Run allowlisted library validation. |
| `pr` | PR record or draft PR | `--plan FILE`, `--approve` | Library release PR. Requires validation to have passed. Never merged. |
| `publish` | registry artifact plus tag | `--plan FILE`, `--approve` | Immutable publish. In production this belongs in CI after merge. |
| `update-consumers` | consumer branches plus PRs | `--plan FILE`, `--approve`, `--dry-run` | Targeted dependency bumps, validated against the published artifact. |
| `status` | nothing | `--plan FILE` | Run state and history. |
| `demo-init` | creates `--dest` | `--dest DIR`, `--scenario independent\|dependent\|conflict\|failing` | Fictional demo environment. |

## Exit codes

| Code | Name | Meaning |
|---|---|---|
| 0 | `OK` | Success, including idempotent `already_*` results |
| 1 | `INTERNAL_ERROR` | Unexpected bug; message reported |
| 2 | `INVALID_INPUT` | Bad arguments, config or manifest |
| 3 | `BLOCKED` | A safety rule stopped progress (`error.reason` says which) |
| 4 | `APPROVAL_REQUIRED` | Write step without `--approve` |
| 5 | `AMBIGUOUS_OR_NOT_FOUND` | Request can't be mapped unambiguously, or the ticket or commits were not found |
| 6 | `OPERATION_FAILED` | Git or integration failure; safe to retry |

## JSON envelope

```json
{
  "ok": false,
  "command": "validate",
  "exit_code": 3,
  "result": {},
  "error": {
    "code": "BLOCKED",
    "reason": "VALIDATION_FAILED",
    "message": "Validation failed; the release is not ready",
    "details": {"status": "FAILED", "results": [{"name": "unit-tests", "exit_code": 1, "output_tail": "..."}]}
  }
}
```

`BLOCKED` reasons: `PLAN_BLOCKED`, `GIT_CONFLICT`, `MANIFEST_MISMATCH`,
`BRANCH_EXISTS_MISMATCH`, `NOT_PREPARED`, `VALIDATION_FAILED`, `NO_VALIDATION_CONFIGURED`,
`NOT_VALIDATED`, `VERSION_COLLISION`, `IMMUTABLE_VERSION`, `VERSION_NOT_PUBLISHED`,
`ARTIFACT_PLAN_MISMATCH`, `CONSUMER_VALIDATION_FAILED`, `BRANCH_NOT_PUSHED`,
`NOTHING_TO_RELEASE`, `UNKNOWN_PLAN`, `PIN_NOT_FOUND`.

## Release manifest (`plan --out`), schema_version 1

| Field | Notes |
|---|---|
| `schema_version` | `1` |
| `plan_id` | 16 hex characters: sha256 of library, base SHA, ordered commit SHAs, expected release patch-ids, version, version file, consumer actions |
| `status` | `PLAN_READY` or `BLOCKED` |
| `library` | `name`, `package_dir`, `version_file` |
| `request` | `kind`, `raw`, `change_keys`, `deferred_keys`, `notes` |
| `release` | `base_ref`, `base_revision` (full SHA), `source_ref`, `source_revision`, `branch`, `apply_order`, `previous_version`, `proposed_version`, `bump`, `reasons`, `collisions`, `status: provisional` |
| `included_changes[]` | `key`, `title`, `commits[]` (`sha`, `subject`, `author`, `patch_id`, `release_patch_id`) |
| `excluded_changes[]` | `key`, `title`, `commits[]`, `reason` |
| `already_released`, `prerequisites` | change keys |
| `analysis` | `verdict`, `findings[]`, `dependency_graph`, `probe`, `public_api_removals`, `confidence_note` |
| `consumers[]` | `name`, `current_version`, `proposed_version`, `action` (`update`, `up_to_date`, `manual`, `not_targeted`, `not_a_consumer`), `reasons`, `rollback_version` |
| `validation` | `required[]` (must equal config), `status: pending` |
| `approval` | `required: true`, `required_for` |
| `remediation[]`, `blockers[]`, `compatibility_risks[]` | |

Validation rules are in `planning/manifest.py::validate_manifest`. A manifest is never
accepted as proof of its own contents: `verify` and `validate` re-derive everything from
Git, and validation commands always come from the config.
