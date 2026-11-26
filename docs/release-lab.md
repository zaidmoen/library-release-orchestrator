# Release lab and scenario matrix

## The lab

`release_orchestrator.lab` generates three repositories with synthetic data. The same
generator feeds the automated tests (local temporary repos) and the GitHub sandbox
(`zaidmoen/harri-release-lab-library`, `-payment`, `-invoice`, all private). Commit
identities and dates are fixed, so the history is identical on every machine.

### Library `zaidlibrary` (trunk = `main`)

Released at `v1.0.0`: `payments.py` (`calculate_total`, `line_total`), `tax.py`
(`tax_for`, regional rates), `invoices.py` (`invoice_due_date`, `format_invoice`),
public exports in `__init__.py`, `pyproject.toml`, and unit tests.

Pending tickets on `main`, oldest first:

| Ticket | Author | Change | Relationship |
|---|---|---|---|
| FIN-198 | Zaid | `currency.py`: `convert` | standalone (change **A**) |
| FIN-204 | Anas | `payments.total_with_tax` (2 commits: feature, tests) | independent of A (change **B**) |
| FIN-211 | Lina | `invoices.format_invoice_in` uses `currency.convert` | **code dependency** on FIN-198 |
| FIN-215 | Omar | bankers rounding in `calculate_total` | changes behaviour |
| FIN-219 | Anas | discounts in `calculate_total`, built on FIN-215 | **textual conflict** without FIN-215 |
| FIN-223 | Sara | `reports.summarize` + test expecting bankers rounding | **semantic dependency** on FIN-215 (cherry-pick is clean, tests fail) |
| FIN-230 | Omar | `refactor!`: removes deprecated `invoice_due_date` | **breaking API** |

Release-candidate tags (cumulative, as a CI that tags trunk would produce):
`v1.1.0-rc.22` at FIN-204 (contains A and B) and `v1.1.0-rc.23` at FIN-211 (A, B, FIN-211).

### Consumers

| Service | Pins | Uses |
|---|---|---|
| `payment-service` | `zaidlibrary==1.0.0` | `calculate_total`, `tax_for` |
| `invoice-service` | `zaidlibrary==1.0.0` | `format_invoice`, `invoice_due_date` |

The Jira fixture contains a ticket whose description holds a shell command (S15).

## Scenario matrix

Every scenario has an automated test in `tests/lab/test_lab_scenarios.py` (LOCAL
INTEGRATION). Running `python examples/lab/run_lab.py` executes them through the CLI and
writes `examples/lab/RESULTS.md`, recording initial state, request, expected outcome,
actual outcome, evidence and verdict. Live GitHub evidence is in
[`docs/evidence/live-github.md`](evidence/live-github.md).

| ID | Scenario | Request | Expected outcome |
|---|---|---|---|
| S01 | Normal release | FIN-198 | `PLAN_READY` → branch → tests pass → draft PR |
| S02 | Independent out-of-order | FIN-204 | `INDEPENDENT`; `currency.py` (A) absent from the branch; tests pass |
| S03 | Dependent | FIN-211 | `DEPENDENT`/`MISSING_PREREQUISITE` on FIN-198; blocked; remediation offered |
| S04 | Git conflict | FIN-219 | `CONFLICT`, `payments.py`, FIN-215 among causes; refs untouched |
| S05 | Semantic dependency | FIN-223 | clean probe, `SHARED_SYMBOL_MODIFIED` warning, validation **fails**, PR/publish refused |
| S06 | Cumulative RC isolation | FIN-204 from `v1.1.0-rc.23` | independent candidate `1.1.0-rc.24` without A and FIN-211; FIN-211 subset blocked |
| S07 | Consumer impact | FIN-204 | only `payment-service` bumped and tested |
| S08 | Failed validation policy | FIN-223 | `pr`, `publish` and `update-consumers` refused |
| S09 | Idempotency | FIN-204 twice | same `plan_id`; `already_*`; one branch, one artifact, one tag, one PR |
| S10 | Concurrent requests | FIN-198 and FIN-204 planned together | both propose `1.1.0`; the second prepare/publish is blocked; re-plan gives `1.2.0` |
| S11 | Breaking API | FIN-230 | `PUBLIC_API_REMOVED`, major `2.0.0`; `invoice-service` validation fails, so no consumer PR |
| S12 | Recovery | FIN-204 with tag creation failing mid-publish | `FAILED` after the artifact is stored; re-run completes, no duplicates |
| S13 | Environment promotion | 1.1.0 through prep → stage → master | ordering, quorum and prerelease rules enforced; idempotent |
| S14 | Rollback | stage at 1.2.0 → 1.1.0 | plan names 1.1.0 with a verified digest; executing appends a record; tags untouched |
| S15 | Security | ambiguous commit, malicious ticket text, unsafe command, unapproved write, tampered manifest, non-allowlisted push | every one rejected |
