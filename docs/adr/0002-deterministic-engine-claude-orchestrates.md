# ADR-0002: Deterministic Python engine; Claude orchestrates

* Status: Accepted
* Date: 2026-11-02

## Context

LLMs are good at reading ambiguous requests and explaining results. They are not a
reliable proof of code independence or version availability.

## Decision

All decisions that affect correctness happen in a typed Python package with a JSON
CLI: Git inspection, dependency verdicts, version selection, manifest verification
and validation status. The Claude Skill normalises requests, calls the CLI, asks for
approval and explains the results. It must never override a `BLOCKED` verdict or
report a step as passed when the CLI did not.

## Consequences

* The engine is unit-testable without Claude.
* The Skill stays short. Detailed policy lives in `references/`.
* The CLI contract (exit codes plus JSON envelope) is a stable interface, documented
  in `docs/cli.md`.
