# ADR-0012: PEP 503 registry adapter with reproducible wheels

* Status: Accepted
* Date: 2026-11-10

## Context

The local directory registry proved the immutability rules but isn't a real package
index, and consumers can't `pip install` from it. The library is a Python package
(`pyproject.toml`, setuptools), so the natural registry protocol is PEP 503 (simple index)
plus the legacy upload API. PyPI, pypiserver, devpi, Artifactory, Nexus and CodeArtifact
all speak it.

Idempotent publication needs "same content gives the same digest". A naive wheel build
embeds the build time, so every retry would look like a conflict.

## Decision

* Artifacts are wheels built from `git archive <release commit>` with
  `SOURCE_DATE_EPOCH` = the commit time, `--no-build-isolation --no-index`. The same commit
  gives a byte-identical wheel (verified: two builds of `release/1.1.0` →
  `00a55821360e5dea…`). The built wheel's version must equal the plan version.
* `PypiRegistry.publish` never trusts an upload response:

  | Situation | Outcome |
  |---|---|
  | A: version absent | upload; then read the index; success only if our sha256 is listed |
  | B: present, same sha256 | `already_published`, no upload |
  | C: present, different sha256 | `ImmutableVersionError` (CONFLICT); the server's own no-overwrite also returns 409 |
  | D: response lost (connection reset) | read the index: ours → success (`reconciled_after: no-response`); absent → bounded retry |
  | E: ambiguous 5xx | same reconciliation by reading the index |
  | F: 401/403 or missing credentials | `RegistryAuthError`, not retried, nothing published |
  | G: concurrent publishers | the index decides: identical content is idempotent, different content conflicts; exactly one content wins |

* Credentials come from environment variables named in config (`username_env`,
  `password_env`) and are never logged. Plain `http` is accepted only for
  `127.0.0.1`/`localhost` test indexes.
* Index registries carry no provenance metadata, so `update-consumers` verifies a
  published artifact by **rebuilding** it from the verified release commit and comparing
  sha256 (`ARTIFACT_PLAN_MISMATCH` otherwise). Downloads are always digest-checked.
* RC versions map to PEP 440 (`1.1.0-rc.24` ↔ `1.1.0rc24`). Other pre-release labels are
  refused.

## Consequences

* Verified against a real, isolated pypiserver in CI (`tests/integration/test_pypi_registry.py`,
  including a full lab release where consumer tests run against the downloaded wheel).
  **No production registry was written.** That needs credentials and explicit
  authorization.
* An RC and its final release are **different wheels** (the version is inside the wheel's
  metadata). The relationship is "same source commit plus a version-only commit", which
  `verify` can check. An automated `finalize` step is future work.
