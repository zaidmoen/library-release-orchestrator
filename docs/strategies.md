# Release strategy comparison

This page compares strategies for the problem "frequently updated shared library,
many consumers, release order ≠ development order".

| Strategy | Works well when | Fails when | Role in this project |
|---|---|---|---|
| **Trunk-Based Development** | Every commit on trunk is releasable; changes are small and land in order of business priority. | The business wants a *later* commit before an *earlier* one. Trunk order is fixed, so you must release both or neither. | Still the development model: developers keep merging to `main`. The orchestrator decouples *releases* from trunk order. |
| **Release branches** | Hotfixes and stabilization for a line (`release/1.x`). | Built naively from `main`, they inherit every earlier commit, including the unwanted A. | Used: each release is cut on an isolated branch **from the last released base**, not from `main`. |
| **Git cherry-pick** | The selected commits are self-contained. | It applies text, not semantics. A clean pick can still call symbols that only exist in excluded commits, and conflicts must be resolved by a human. Commit identity changes, so provenance is lost unless recorded. | Used **only after** dependency analysis says the change is independent, always with `-x`, and verified afterwards with `patch-id`. Never treated as proof of independence. |
| **Feature flags** | Unfinished work can ship dark, so release order stops mattering for *code*. | Libraries have many consumers. Flags spread configuration to every service, dead code piles up, and flags don't help with API removals. Needs runtime configuration discipline. | Offered as a **remediation option** when B depends on A: ship A behind a flag, then release B. |
| **Modular library architecture** | Independent concerns live in independent modules or packages with their own versions. | The modules share core types. Splitting too finely causes a version matrix explosion and diamond dependencies. | A long-term recommendation. The analysis finds which modules each change touches, and that data is what a split needs. |
| **Independent component versioning** | Monorepo with clear package boundaries (for example `zaidlibrary-payments`, `zaidlibrary-invoices`). | Cross-package changes need coordinated releases, which brings back the same ordering problem one level up. | Planned extension: the version manager already works per library name. |
| **Dependency-aware release planning** | You need to pick an arbitrary subset of pending changes and know whether it is coherent. | Dependencies are invisible to static analysis (runtime, data, configuration). | **The core of this project.** Static symbol analysis, a sandbox apply probe, a graph with cycle detection, and validation as the backstop. |
| **Immutable artifact publishing** | Always. Consumers must be able to trust that `1.1.0` means one exact thing. | Teams "fix" a bad release by re-uploading the same version. | Enforced: a version is published once, keyed by digest, and a bad release is superseded by a new version, never replaced. |
| **Semantic Versioning** | Consumers decide upgrade risk from the version number alone. | Versions get assigned by *timeline* instead of *content*, or a breaking change is shipped as a minor release. | Enforced: the version is derived from the **content of this release** (conventional-commit types plus removed-public-API detection) and the **highest published version**, not from the base or the branch. |

## Why dependency-aware planning plus isolated release branches plus immutable artifacts

A frequently updated shared library needs three properties at the same time:

1. **Selective release:** ship B without A. Trunk-based development alone can't do
   this.
2. **Evidence that the selection is coherent:** cherry-pick alone can't provide it.
   Static analysis, an apply probe and tests together give layered evidence, and the
   result is stated as uncertain wherever the evidence is incomplete.
3. **Traceable, immutable output** that consumers can adopt at their own pace:
   semantic versioning plus immutable artifacts plus targeted consumer PRs.

Feature flags and modularisation are kept as **remediations** the planner proposes
when a selective release is unsafe. The planner doesn't force them on every change.

## Example: out-of-order release with versions

```
published: 1.0.0 (base)
main:      1.0.0 - A(FIN-198, feat) - B(FIN-204, feat)

Release 1: base v1.0.0 + B              -> 1.1.0   (minor: feat)
Release 2: base release/1.1.0 + A       -> 1.2.0   (minor: feat)
```

For Release 2 the planner is pointed at the 1.1.0 release commit as its base
(`--base`). Changes whose commits already exist in the base (same `patch-id`) are
reported as `already_released` and aren't applied twice.
