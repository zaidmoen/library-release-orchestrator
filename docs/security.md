# Security model

## Trust boundaries

| Input | Trust | Handling |
|---|---|---|
| `release-orchestrator.yaml` (config) | **Trusted** (code-reviewed, owned by release engineering) | Only source of validation commands, repository paths and provider choice. Validated on load. |
| Jira ticket text (summary, description) | Untrusted | Used as display text only. Never executed, never used as a path, branch name or command argument. Only the ticket **key** and the structured `consumers` field are consumed, and both are checked against strict patterns or configured names. |
| Commit messages, trailers | Untrusted | Parsed with fixed regexes for change keys and conventional-commit types only. |
| PR text / comments | Untrusted | Not read by the engine. |
| Natural-language requests | Untrusted | Only change keys (regex) and configured aliases are extracted. Anything else gives `AmbiguousRequestError`. |
| Repository code | Untrusted | Parsed with `ast.parse` (never imported or executed during analysis). Executed only by the allowlisted validation commands, in a temporary export, with a timeout. |

## Controls

* **No shell:** every subprocess call uses an argv list with `shell=False`. The Git
  wrapper refuses a denylist of operations (`push`, `merge`, `reset --hard`,
  `branch -D`, `clean`, `--force`).
* **Ref-name validation:** generated branch names are checked with
  `git check-ref-format --branch`, and change keys must match the configured
  pattern.
* **Approval gates:** every write command exits with code `4`
  (`APPROVAL_REQUIRED`) unless `--approve` is passed. The Skill asks the human
  before passing it.
* **Credentials:** read only from environment variables (`GITHUB_TOKEN`). Never
  written to plans, reports, logs or exceptions. The GitHub adapter redacts the
  `Authorization` header in its error messages.
* **Remote writes:** only `Git.push_ref`, called from the approved `push` step, may push.
  It sends one `refs/heads/*` or `refs/tags/*` ref, refuses `+`/`:` refspecs and
  force/delete/mirror flags, and only targets remotes whose normalised URL is in the
  config's `remote_allowlist`. A remote tag at another commit is never overwritten
  (ADR-0008, S15 tests).
* **Environment changes** need `--approve` plus a quorum of distinct approver names. These
  names are self-declared in this version, a known gap.
* **No destructive Git operations:** no force-push, no merge, no branch deletion,
  no `reset --hard`, no `stash`. Cleanup removes only temporary directories created
  by the current process.
* **Immutable versions:** the registry refuses to overwrite a published version.
* **Validation command placeholders** are limited to `{python}` (the current
  interpreter) and `{library_path}`. Values are passed as argv elements and never
  re-parsed by a shell.

## Known gaps (POC)

* Validation commands run with the user's privileges. Production should run them in
  CI or a container sandbox.
* The fixture issue tracker trusts its YAML file. A live Jira adapter must use a
  read-only service account.
* There is no signature or provenance attestation on artifacts (SLSA/Sigstore) yet
  **[planned]**.

## Security review (2026-11-10)

| Risk | Control | Test |
|---|---|---|
| Registry URL prefix tricks (`http://localhost.evil.example`) | URL parsing plus exact loopback hostnames; https elsewhere | `test_unsafe_registry_urls_are_rejected` |
| Credentials embedded in URLs or config values | rejected; config holds env var **names** only | `test_credentials_must_be_env_var_names_not_values` |
| `True` accepted as an approval count | `_is_int` excludes `bool` everywhere numbers are parsed | `test_required_approvals_rejects_bools_and_out_of_range`, `test_bools_are_never_numbers` |
| Typos silently weakening policy | unknown `approval_policy` keys and duplicate checks rejected | `test_policy_rejects_duplicate_checks_unknown_keys_and_bad_permission` |
| Mislabelled production index | explicit `production` flag; non-prod only for loopback/allowlisted hosts | `test_remote_index_must_be_explicitly_classified` |
| Approval by an unprivileged or unverifiable reviewer | verified permission ≥ `write`, else not counted | `test_approval_without_verified_write_permission_does_not_count` |
| Dismissed / stale approvals | latest decisive review per reviewer, head-commit match | `test_dismissed_review_revokes_earlier_approval`, `test_stale_approval_on_older_commit_does_not_count` |
| Treating unreadable branch protection as "none" | recorded as unverified; can be required | `test_unreadable_branch_protection_is_never_treated_as_unprotected` |
| GitHub approval mistaken for production authorization | production registry always refused | `test_production_registry_is_refused_even_with_verified_github_approval` |
| Index redirecting downloads to another host | same-origin check on every file link | `test_index_links_to_a_foreign_host_are_refused` |
| Lost updates between processes | compare-and-swap revisions; `O_EXCL` lock (Windows delete-pending handled) | `test_run_state_concurrency.py` |
