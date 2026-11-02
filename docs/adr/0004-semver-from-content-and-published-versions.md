# ADR-0004: Versions derive from release content and the highest published version

* Status: Accepted
* Date: 2026-11-02

## Context

With out-of-order releases, "the next version after the base" is wrong. The base
may be `1.0.0` while `1.1.0` has already been published from another selection.

## Decision

* Parse strictly with an in-house SemVer 2.0 implementation (small and exhaustively
  tested). `packaging.version` implements PEP 440, not SemVer: it accepts `1.0`,
  normalises `1.0.0-rc1` to `1.0.0rc1`, and has different pre-release semantics.
  Adding the `semver` package for about 80 lines of logic isn't worth the extra
  supply-chain surface.
* Bump level = max over included commits: `BREAKING CHANGE:` footer or `type!:`
  gives major, `feat` gives minor, anything else gives patch. Removing a public
  top-level symbol (no leading underscore) from the library forces a major bump.
* Version = bump(max(registry versions ∪ release tags)). If the candidate still
  collides (for example a tag without an artifact), the plan stops with
  `VERSION_COLLISION`. It never silently picks another number.
* The plan's version is **provisional**. It becomes final only after a successful
  publish.

## Consequences

* Release 2 in the A/B example gets `1.2.0` even though its base is `1.1.0`.
* PEP 440-only forms (`1.0.post1`, epochs) aren't supported **[planned]**.
