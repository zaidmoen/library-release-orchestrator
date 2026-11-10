# Live GitHub validation (2026-11-08)

Validation of the orchestrator against **real GitHub repositories**, using the release
lab. Every value below was observed with `git ls-remote`, the CLI's JSON output, or Git
on the clones that pushed to GitHub. Nothing is inferred from docs or unit tests.

| Repository | URL |
|---|---|
| Library | https://github.com/zaidmoen/harri-release-lab-library (private) |
| payment-service | https://github.com/zaidmoen/harri-release-lab-payment (private) |
| invoice-service | https://github.com/zaidmoen/harri-release-lab-invoice (private) |

**What "live" means here.** Branches, tags and PRs are real GitHub objects. The **package
registry is the local directory registry** (`registry/`), not a production registry. Its
digest and immutability are real, but nothing was uploaded to PyPI, Artifactory or
CodeArtifact. Validation commands ran on the operator's machine, not in GitHub CI.

## 1. Seeded state (`examples/lab/publish_lab_to_github.py --yes`)

| Repo | Ref | SHA |
|---|---|---|
| library | `main` | `f9dff80a2191c450445970c2f1ac6b3a52d6f4fe` |
| library | `v1.0.0` | `84fc6b4b03392093f2a17dfa908a1edffa09683a` |
| library | `v1.1.0-rc.22` | `319e010f133fa1899cbc289c2e93956b44af37aa` |
| library | `v1.1.0-rc.23` | `dc3dbfcf887573513027e43e04c4c23613765d55` |
| payment | `main` | `807a32a23c218f6ba752ad99ebc3fffe3fac7af5` |
| invoice | `main` | `72fe57e88230fdbaae78593470d2ba6ad883600e` |

## 2. Scenarios run live

| ID | Request | Expected | Actual | Exit | Verdict |
|---|---|---|---|---|---|
| L-S03 | `plan --ticket FIN-211` | Blocked: needs FIN-198; nothing written | `BLOCKED`, `MISSING_PREREQUISITE` on FIN-198 (`zaidlibrary.currency.convert`); remote refs unchanged | 3 | PASS |
| L-S02 | `plan --ticket FIN-204` | B without A, `1.1.0` | `PLAN_READY`, verdict `INDEPENDENT_WITH_RISKS` (warning: `calculate_total` is modified by later tickets); excluded FIN-198, 211, 215, 219, 223, 230 | 0 | PASS |
| L-S02 | `prepare --approve`, `validate` | Isolated branch, tests pass | `prepared`; `unit-tests` **PASSED** (exit 0) | 0 / 0 | PASS |
| L-S02 | `push --approve` | Branch on GitHub, not forced | `refs/heads/release/1.1.0` pushed at `2c8e859e474c63d8ee931326950d0d70df8f8e10` | 0 | PASS |
| L-S02 | `publish --approve`, `push --approve` | Immutable artifact plus tag | `published`, sha256 `86f5a3edd5b99d0b…` (local registry); `refs/tags/v1.1.0` pushed at `2c8e859e47…` | 0 / 0 | PASS |
| L-S07 | `update-consumers --approve`, `push --approve` | Only payment-service bumped and tested | payment-service `prepared`, validation **PASSED** against the published 1.1.0; `deps/zaidlibrary-1.1.0` pushed at `51efa021da804f3c0ac036fc66b1f1b4cf3b5df5`; invoice-service `not_targeted` | 0 / 0 | PASS |

## 3. Release integrity (re-derived from Git, `verify --plan plan.json`: exit 0, all checks PASS)

* Base `v1.0.0` = `84fc6b4b03…`. The branch holds exactly **3** commits:

  | Release commit | Source commit | Author | patch-id (source = release) |
  |---|---|---|---|
  | `c27f59138d` | `3bf1cee495` FIN-204 feat | Anas | `a5db4c7d8a97` = `a5db4c7d8a97` |
  | `7a935f846e` | `319e010f13` FIN-204 tests (`Refs:` trailer) | Anas | `bb1364a2f209` = `bb1364a2f209` |
  | `2c8e859e47` | version bump to `1.1.0` (orchestrator) | bot | n/a |

* Each cherry-pick carries `(cherry picked from commit <sha>)`.
* Files changed against `v1.0.0`: `src/zaidlibrary/__init__.py` (version),
  `src/zaidlibrary/payments.py` (+8), `tests/test_total_with_tax.py` (+9).
  **`src/zaidlibrary/currency.py` (FIN-198) is absent.**
* `validate-manifest`: valid; `plan_id` `1df1f7dc784437b0` matches the content.
* Tag `v1.1.0` and `release/1.1.0` point at the same commit, `2c8e859e47…`.

## 4. Idempotency (full re-run of the workflow against live GitHub)

| Step | Result | Exit |
|---|---|---|
| `plan --ticket FIN-204` | first run: `BLOCKED/NOTHING_TO_RELEASE` (defect, see §6); after the fix: `ALREADY_RELEASED` in `v1.1.0` (`2c8e859e47`) | 3 → **0** |
| `prepare --approve` | `already_prepared` | 0 |
| `validate` | `PASSED` | 0 |
| `pr --approve` | existing request returned (`created: false`) | 0 |
| `publish --approve` | `already_published`, same digest `86f5a3edd5b99d0b…` | 0 |
| `update-consumers --approve` | payment-service `already_prepared` | 0 |
| `push --approve` | every ref `already_pushed` | 0 |

Before/after comparison: `git ls-remote` output for **all three repositories was
byte-identical**. Local branches, tags, registry files (`1.1.0.json`, `1.1.0.tar`) and
PR-request count (2) were identical, and `plan.json` was byte-identical. No duplicate
release, tag, artifact or PR request was created.

## 5. Topology review: release PR target

GitHub reported `release/1.1.0` and `main` as **diverged**:
`git rev-list --left-right --count main...release/1.1.0` = **8 behind, 3 ahead**,
merge-base `v1.0.0`. A release PR into `main` isn't a meaningful merge target for an
out-of-order release (ADR-0009). Fix: release PRs target `release-base/<base>`, pinned at
the release base. With the user's approval, `refs/heads/release-base/v1.0.0` was pushed at
`84fc6b4b03…` (= `v1.0.0`). It was the only ref added; no existing ref moved. The PR diff
`release-base/v1.0.0...release/1.1.0` is exactly the 3 files above.

## 6. Defects found live, and their fixes

| Defect | Root cause | Fix | Regression test |
|---|---|---|---|
| Re-planning an already released change exited 3 (`BLOCKED`) | `NOTHING_TO_RELEASE` was raised as a blocker | `plan` returns `ALREADY_RELEASED` (exit 0) with `released_in` and source commits | `test_replan_after_release_is_recognised_not_blocked` |
| Release PR targeted the diverged trunk | PR base hard-coded to `source_ref` | `release_pr_target: integration` (default), never moves an existing integration branch | `test_release_pr_targets_integration_branch_at_release_base`, `test_existing_integration_branch_elsewhere_is_never_moved` |
| Unsubmitted PR request couldn't be corrected | request file was write-once | corrected while no PR number is recorded; recorded PRs never retargeted | `test_unsubmitted_external_pr_request_can_be_corrected` |
| GitHub MCP connector couldn't create repos (403) or read the lab repos (404) | Claude GitHub app has no permission to create repos or access these private repos | Repos created by a human; PRs via prefilled compare links (human-submitted), verified through `refs/pull/*` | n/a (environment) |

## 7. Pull requests: **BLOCKED (human GitHub step)**

No PR exists on any lab repository. Evidence, checked repeatedly up to 2026-11-08T16:37Z:

* `git ls-remote <lab repo> 'refs/pull/*'` returns nothing for all three repos. As a
  control, the same command on `zaidmoen/library-release-orchestrator` returns
  `refs/pull/1/head` (`8fed533dc6…`), so the method does detect PRs.
* The GitHub MCP connector gets 403 when creating repos and 404 when reading the lab repos,
  because the Claude GitHub app has no access to them.
* The repos are private: the anonymous API returns 404. On GitHub Free, **draft PRs aren't
  available for private repositories**, which most likely explains why the
  "create draft PR" submissions never produced a PR.

What is ready and verified: both PR requests exist (`.lro-state/pr-requests/`), and both
head branches and the library base branch are on GitHub:

| PR (to be created) | Head → base | Head SHA | Expected diff |
|---|---|---|---|
| library | `release/1.1.0` → `release-base/v1.0.0` | `2c8e859e47` | 3 files (`__init__.py`, `payments.py`, `test_total_with_tax.py`) |
| payment-service | `deps/zaidlibrary-1.1.0` → `main` | `51efa021da` | `requirements.txt`: `zaidlibrary==1.0.0` → `1.1.0` |

To finish: make the repos public (or use a plan with private draft PRs), create both
draft PRs from the prefilled compare links, then run
`release-orchestrator record-pr --plan plan.json --repository <name> --head <branch> --number N --url URL`
and re-run `pr` / `update-consumers` to confirm the recorded PRs are reused, not duplicated.

## 8. Safety verification

| Constraint | Evidence |
|---|---|
| No merge | No PR exists and none was merged; the engine has no merge path |
| No force-push | Every push went through `Git.push_ref` (single ref, no `+`). The ref diffs above show additions only |
| No branch or tag deletion or move | `ls-remote` before and after: no ref removed or changed |
| invoice-service untouched | Only `refs/heads/main` at `72fe57e882…`, the same before and after every step |
| Approvals | Repos created by a human; each new remote write was approved in chat |
| No real company repositories | Only the three `harri-release-lab-*` sandbox repos (synthetic data) were written |

## 9. Remaining risks and limits

* The registry is local. Production immutability and concurrency depend on the real
  registry's create-if-absent behaviour.
* Validation ran locally, not in CI on GitHub (the lab repos have no workflows).
* The GitHub MCP connector can't access private sandbox repos without an app permission
  change. The Python REST adapter is still MOCKED (no token).
* The `INDEPENDENT_WITH_RISKS` warning for FIN-204 is covered by the library's tests only.


## 10. PR reconciliation, live (2026-11-10, ADR-0010)

| Check | Command | Result | Exit |
|---|---|---|---|
| Lab PR state | `reconcile-prs --plan plan.json` | `zaidlibrary` `release/1.1.0` (head `2c8e859e47`): **not_created**; `payment-service` `deps/zaidlibrary-1.1.0` (head `51efa021da`): **not_created** | 0 |
| Read-only | `git ls-remote` before and after on all three lab repos | identical | n/a |
| Positive control (live) | `remote_pull_requests()` on `zaidmoen/library-release-orchestrator` | `{1: 8fed533dc6}` → `https://github.com/zaidmoen/library-release-orchestrator/pull/1`, matching the merged branch tip | n/a |
| GitHub MCP access to lab repos | `list_branches harri-release-lab-library` | **404** (app has no access; repos stay private by instruction) | n/a |

Lab PR creation remains **BLOCKED** (see §7). Once a PR exists, `reconcile-prs` records it
automatically, so no manual `record-pr` is needed.

## 11. Authoritative PR status, live (2026-11-10, ADR-0011)

`release-orchestrator github-pr-status --repository zaidmoen/library-release-orchestrator --number 2 --required-check lint --required-check "test (ubuntu-latest, 3.12)"`
(anonymous REST, read-only), exit 0:

| Field | Value read from GitHub |
|---|---|
| state / draft / merged | `open` / `true` / `false` |
| head → base | `feat/real-world-release-lab` (`72a7a4617613…`) → `main` |
| reviews | none |
| check runs on head | 10 runs (push and pull_request workflows), all `completed/success` |
| decision | **not approved**: "PR #2 is still a draft", "0 current approval(s), 1 required" |

This is the expected, correct result: the engine refuses to treat a draft PR with no
review as an approved release. Private lab repos can't be read the same way without a
token (`GITHUB_ACCESS_DENIED`, 404), and the connector also gets 404. **Blocked.**

## 12. Jira (2026-11-10, ADR-0013)

The Atlassian MCP server is connected with read-write Jira access to the company's real
site, not a sandbox. The FIN-* lab tickets are fictional and aren't there. No Jira data was
read into this repository and nothing was written to Jira. Live Jira eligibility is
**BLOCKED** pending a sandbox project or explicit read-only authorization. The engine side
is verified locally (`tests/unit/test_issue_eligibility.py`).
