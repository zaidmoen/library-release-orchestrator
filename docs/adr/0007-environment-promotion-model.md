# ADR-0007: Environment promotion through an immutable artifact ledger

* Status: Accepted
* Date: 2026-11-08

## Context

Releases move through `prep → stage → master`. Teams often model these as long-lived
Git branches and promote by cherry-picking or merging into them. With out-of-order
releases that model breaks down:

* Branches drift. Whatever is on `stage` is "the commits someone picked", not a
  versioned, tested artifact.
* Promoting out of order means rewriting or force-moving branches, which is forbidden.
* An RC number on a branch says nothing about which changes it contains.

## Options considered

| Model | Pros | Cons |
|---|---|---|
| Environment branches (`prep`, `stage`, `master`) | Familiar; the diff between envs is visible in Git | Out-of-order promotion needs cherry-picks or force-moves; what's deployed isn't an artifact; drift |
| Mutable "environment tags" (`env/stage`) | Simple | Moving tags violates immutability; history of the env is lost |
| **Append-only environment ledger pinning immutable artifacts** | Each env points at one published version and digest; full history; out-of-order is a policy decision, not a Git operation | One more state store to protect (in production: a database or a Git-backed config repo with PR review) |

## Decision

Environments are defined in config as an **ordered list with policies**:

```yaml
environments:
  - name: prep
    required_approvals: 1
    allow_prerelease: true
  - name: stage
    required_approvals: 1
    allow_prerelease: true
  - name: master
    required_approvals: 2
    allow_prerelease: false
```

* Promotion appends a record (version, sha256 digest, approvers, time, previous version)
  to `<state_dir>/environments/<env>.json`. Records are never edited or removed.
* A version can be promoted to an env only if it's **published** (digest verified) and,
  for every env except the first, it's **already current or previously promoted in the
  preceding env**. Skipping stage is impossible.
* `allow_prerelease: false` keeps RC versions (`1.2.0-rc.23`) out of `master`. A final
  version must be published from the same release commit.
* `required_approvals` is a quorum of **distinct** approver names. Approver identity is
  self-declared on the CLI in this version. In production it must come from an
  authenticated system (PR reviews or a deploy tool). That gap is called out explicitly.
* Promotion is idempotent: promoting the version that's already current returns
  `already_current`.
* **Rollback is roll-forward to a previous artifact.** `rollback-plan` finds the previous
  distinct version in the env's ledger, verifies its artifact still exists with the same
  digest, and lists consumer re-pins. Executing it appends a new record. History,
  published versions and tags are never rewritten.
* The orchestrator **never deploys**. A ledger record is the hand-off to a deploy
  system, which stays out of scope.

## Consequences

* Branch-based environments (if a team keeps them) can be fed from the ledger by their
  own tooling. The orchestrator doesn't move them.
* The local JSON ledger has single-filesystem atomicity only (same caveat as ADR-0005).
