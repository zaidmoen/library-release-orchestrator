# ADR-0014: Interactive release modes over the deterministic engine

* Status: Accepted
* Date: 2026-11-16

## Context

Engineers ask for releases in three shapes: prepare a release candidate, finalize a
candidate into a stable release, or change the order of pending candidates ("rc.23 before
rc.22"). Each needs the same disciplined loop: discover the real state, decide how to
handle dependencies, choose where to apply the change, preview the impact, and only then
execute with approval. The Skill must guide this conversation without becoming the
authority on safety.

## Decision

* One read-only engine, `release_orchestrator.modes.ModeEngine`, exposed as
  `release-orchestrator release-mode discover | options | preview | check-preview`. It
  reuses `scan_history`, `build_plan`, `DependencyAnalyzer`, `check_eligibility`, the
  registry, the environment ledger and the GitHub/Git adapters. No release logic is
  duplicated in the Skill.
* **Discovery** derives pending RCs from tags: a pre-release newer than the latest stable
  release, whose final version isn't tagged or published. For each RC it records its
  tickets (Git range from the stable tag), which other RCs it **contains** (Git
  ancestry), and its **own** tickets. Missing integrations (no remote, no tracker, no
  branch mapping, unreadable registry, inaccessible repo) become explicit
  `uncertainties`.
* **Q1 options** are generated from evidence:
  * `release-candidate`: the request as-is if independent; otherwise *requested-only*
    (blocked, with the blockers) and *with-prerequisites* (the code-analysis closure,
    re-validated).
  * `release`: one option per pending candidate. The final version must equal the
    candidate's core version (`VERSION_PROGRESSION` otherwise), and execution includes
    `verify-lineage`: the final release may differ from the candidate's commit **only in
    the version file**.
  * `reorder-releases`: the requested order, the ancestry ("dependency") order, and
    *supersede* (release only the newest candidate when it contains all the others).
    Each order is simulated step by step: a candidate's own tickets are planned together
    with everything released before it. The first blocked step blocks the order and later
    steps are reported `SKIPPED`. Versions progress from the planner's bump level
    (1.1.0 → 1.2.0), never reused.
* **Q2 (services)** and **Q3 (environments)** scope the operation only. Affected but
  unselected services stay in the preview as `NOT SELECTED`, with a warning.
  `update-consumers --consumer` enforces the selection at execution time. Omitting Q3
  means **no environment operation**.
* **Preview** statuses:
  * `READY`: requires real evidence (a passed `validate` on the exact plan ID, plus a
    verified approval when a policy exists);
  * `NEEDS VALIDATION`: deterministic checks pass but evidence is missing;
  * `BLOCKED`: a blocker, an inaccessible repo, an RC aimed at a no-prerelease env, or
    promotion out of order;
  * `NOT SELECTED`: the service wasn't selected.
* Every preview carries a **fingerprint**: tag SHAs, source SHA, published versions,
  consumer pins and environment versions. `check-preview` fails with `STALE_PREVIEW` if
  any of them changed. A preview is never approval: execution goes through the existing
  `--approve`-gated commands, each confirmed by a human.

## Consequences

* Multi-repository operations are **not atomic**. The preview orders them, and each step
  is idempotent and resumable (ADR-0005, ADR-0010).
* Branch mappings come from `environments[].branch` in config. Promotion itself stays
  ledger-based (ADR-0007), and the orchestrator doesn't move environment branches.
* Reorder analysis requires candidates to be on the source branch. Otherwise the option
  is reported as unsupported rather than guessed.
