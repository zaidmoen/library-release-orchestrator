# ADR-0001: Dependency-aware release planning over blind cherry-pick

* Status: Accepted
* Date: 2026-11-02

## Context

The business needs to release changes in an order that differs from commit order.
Cherry-picking B onto the last release can succeed textually while B depends on
code that only A introduced.

## Decision

Release candidates are produced in this order:

1. Group commits into logical changes by business key.
2. Analyse dependencies between the requested changes and the excluded ones that
   sit between the base and the source branch, using static Python symbol and
   module analysis plus shared-file and hunk-overlap detection.
3. Probe applicability with a real `cherry-pick` in a **throwaway clone**.
4. Plan only when there are no blocking findings, and require validation before
   PR readiness.

A successful cherry-pick is recorded as *evidence of textual applicability*, never
as *proof of independence*.

## Consequences

* Some false positives are expected (conservative blocking). Every finding is
  explained and the planner offers remediations.
* False negatives remain possible for runtime or data dependencies. Validation is
  mandatory, and Scenario F demonstrates that it catches what static analysis can't.
