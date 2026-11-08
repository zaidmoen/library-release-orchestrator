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
