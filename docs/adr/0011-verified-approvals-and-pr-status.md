# ADR-0011: Publication requires a GitHub-verified approval of the exact release commit

* Status: Accepted
* Date: 2026-11-10

## Context

`--approve` records that the *operator* agreed to run a step. It doesn't prove that a
reviewer approved the release, and approval names typed on the CLI can be spoofed. Git
refs (ADR-0010) show that a PR exists, but not its state, reviews or checks.

## Decision

* `GitHubReader` (read-only REST) fetches PR state, draft, merged, head and base, reviews
  (with `commit_id`) and check runs. A token comes from `GITHUB_TOKEN` (read access to PRs
  and checks is enough). Public repositories work anonymously. 401/403/404 are final
  errors; network failures, 5xx and rate limits are retried at most 3 times.
* `evaluate_approval` is a pure, deterministic function:
  * The PR must be open, not merged, and (by default) **not a draft**.
  * The PR head must equal the **verified release commit** (`verify` against the plan).
  * Only each reviewer's latest `APPROVED` / `CHANGES_REQUESTED` / `DISMISSED` review
    counts. An approval counts only if its `commit_id` is the current head: approvals of
    earlier commits are **stale**. The PR author never counts. Any open change request
    blocks.
  * Every required check must have **completed with `success`** on the head commit. When a
    check ran more than once, the most recently started run is used.
* With `approval_policy` configured, `publish` runs `pr_status` and refuses with
  `APPROVAL_NOT_VERIFIED` unless the decision is approved. The full evidence (PR snapshot,
  reviews, checks, decision) is stored in the run state. `--approve` is still required as
  well: the operator's consent and the reviewers' approval are separate gates.
* **Fail closed on anything unverifiable** (security review, 2026-11-10):
  * An approval counts only if the reviewer's repository permission is verified at
    `min_reviewer_permission` (default `write`) or above. If the permission API can't be
    read (anonymous or under-scoped token), the approval doesn't count.
  * A later `DISMISSED` review revokes that reviewer's approval.
  * Required checks are satisfied by the latest check run **or** a legacy commit status;
    `pending`, `error`, `failure` and missing all block.
  * Branch protection is read when the token allows. Its required checks are **added** to
    the policy's. If it can't be read, the evidence says `unverified (<status>)`, never
    "unprotected". With `require_branch_protection_evidence: true` that blocks.
* **Production is a separate authorization.** A registry with `production: true` is
  always refused (`PRODUCTION_PUBLISH_UNAUTHORIZED`), *even with* a verified GitHub approval.
  Review approval authorizes code, not production publication, and no production
  authorization source (for example a GitHub deployment environment with required
  reviewers) is implemented yet.

## Authorization boundaries

| Action | Needs |
|---|---|
| plan, analyze, verify, validate, reconcile, status | nothing (read-only) |
| prepare, push, pr, update-consumers | operator `--approve` |
| publish to a loopback/test registry | `--approve` (+ verified approval when a policy is set) |
| publish to a production registry | **refused**: no production authorization source exists |
| promote / rollback environments | `--approve` + quorum (self-declared names: known gap) |
| merge, deploy | never performed by the engine |

## Consequences

* Verified live: PR #2 on this repository was read through the adapter (draft, open, head
  `72a7a46`, all checks `success`, 0 reviews), and the decision was "not approved: draft,
  0 approvals". See docs/live-github-validation.md.
* Branch-protection rules aren't read (that needs admin scope). The policy is configured
  in this repository instead, a deliberate least-privilege choice.
