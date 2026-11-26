# Live GitHub evidence

The full record is in [../live-github-validation.md](../live-github-validation.md).
The assessment is in [../engineering-assessment.md](../engineering-assessment.md).

| Area | Status (2026-11-08) |
|---|---|
| Lab repos seeded (`main`, tags) | VERIFIED LIVE |
| Out-of-order release branch, tag, consumer branch pushed | VERIFIED LIVE |
| Dependent change blocked with no remote write | VERIFIED LIVE |
| Idempotent re-run (remote refs byte-identical) | VERIFIED LIVE |
| Draft PRs (library, payment-service) | **BLOCKED**: no `refs/pull/*` exists yet (human GitHub step) |
| Python REST adapter | MOCKED |
