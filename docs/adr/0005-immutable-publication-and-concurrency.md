# ADR-0005: Immutable publication; concurrency is the registry's job

* Status: Accepted
* Date: 2026-11-02

## Context

Two engineers may prepare releases at the same time and both compute `1.1.0`.

## Decision

* A local or in-memory lock is **not** claimed to provide global safety.
* Publication is the single point that makes a version real. A registry adapter
  must give *create-if-absent* semantics. `LocalRegistry` uses
  `os.open(path, O_CREAT | O_EXCL)` on the version record, which is atomic within
  one filesystem. Production registries (PyPI, Artifactory, CodeArtifact) reject
  re-uploads of an existing version, which gives the same guarantee across machines.
* The loser of a race gets `ImmutableVersionError` and must re-plan. The plan ID
  changes because the version is part of the plan content.
* Re-publishing the same version with an identical artifact digest is idempotent
  (`already_published`).

## Consequences

Two concurrent prepares can each create a release branch for `1.1.0` (with
different branch names if their contents differ), but only one artifact can ever
be published as `1.1.0`. A test publishes the same version from several threads and
checks that exactly one wins.
