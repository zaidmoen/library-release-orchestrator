# Interpreting findings and remediation

| Verdict | Meaning | What to tell the user |
|---|---|---|
| `INDEPENDENT` | No static dependency on excluded changes, and the sandbox apply is clean. | Safe to continue. Tests still decide. |
| `INDEPENDENT_WITH_RISKS` | Warnings only (shared files, a symbol modified by an excluded change, non-Python files). | Continue, but point out each warning. Validation is the real check. Scenario F is the example where it fails. |
| `DEPENDENT` | The requested change uses a symbol or module that only an excluded change introduces. | Releasing it alone would break at import or runtime, even though `git cherry-pick` succeeds. Offer the remediation options. |
| `CONFLICT` | Commits don't apply on the base. | A human must make a backport commit. Never auto-resolve conflicts or drop code. |
| `UNKNOWN` | Ambiguous commit mapping or another blocker. | Ask for clarification. |

## Remediation options produced by the planner

* `include_prerequisites`: re-plan with all keys in `changes`
  (`LRO plan --ticket A --ticket B`).
* `extract_shared_part`: the developer moves the needed code into its own small change.
* `feature_flag_prerequisite`: ship the prerequisite disabled and enable it later.
* `release_in_development_order`: release the prerequisite first, as it was developed.
* `backport_commit` (conflicts): the change author creates a backport on the release base.
* `include_conflicting_changes`: release together with the conflicting change.
* `clarify_commit_mapping`: split a commit that references several tickets.
* `investigate_version_collision`: a tag or artifact already uses the version. Re-plan
  after investigating. Never overwrite.

Present these as options. The choice belongs to the business owner and the engineers,
not to you.
