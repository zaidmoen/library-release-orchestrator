"""Version recommendation policy (ADR-0004)."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from release_orchestrator.models import BumpLevel, CommitInfo
from release_orchestrator.versioning.semver import SemVer

INITIAL_VERSION = SemVer(0, 1, 0)
_CC_RE = re.compile(r"^(?:[A-Z][A-Z0-9]+-\d+:?\s*)?(?P<type>[a-z]+)(?:\([^)]*\))?(?P<bang>!)?:")
_BREAKING_RE = re.compile(r"^BREAKING[ -]CHANGE:", re.MULTILINE)


def bump_for_commit(commit: CommitInfo) -> tuple[BumpLevel, str]:
    """Conventional Commits: ``type!:``/``BREAKING CHANGE:`` -> major, ``feat`` -> minor."""
    match = _CC_RE.match(commit.subject)
    if (match and match.group("bang")) or _BREAKING_RE.search(commit.body):
        return BumpLevel.MAJOR, f"{commit.short}: breaking change marker"
    if match and match.group("type") == "feat":
        return BumpLevel.MINOR, f"{commit.short}: feat"
    kind = match.group("type") if match else "unclassified"
    return BumpLevel.PATCH, f"{commit.short}: {kind}"


@dataclass(frozen=True)
class VersionDecision:
    previous: SemVer | None
    proposed: SemVer
    bump: BumpLevel
    reasons: tuple[str, ...]
    collisions: tuple[str, ...] = field(default_factory=tuple)

    @property
    def collides(self) -> bool:
        return bool(self.collisions)

    def to_dict(self) -> dict[str, object]:
        return {
            "previous_version": str(self.previous) if self.previous else None,
            "proposed_version": str(self.proposed),
            "bump": str(self.bump),
            "reasons": list(self.reasons),
            "collisions": list(self.collisions),
            "status": "provisional",
        }


def recommend_version(
    commits: Iterable[CommitInfo],
    *,
    published: Iterable[SemVer],
    tagged: Iterable[SemVer],
    api_removals: Iterable[str] = (),
    initial: SemVer = INITIAL_VERSION,
    prerelease: str | None = None,
) -> VersionDecision:
    """Recommend the next version.

    With ``prerelease`` (e.g. ``"rc"``) the result is ``<target>-rc.<N>`` where ``<target>``
    is the next final version and ``N`` is one more than the highest existing ``rc`` for
    that target. RC numbers only order candidates; they say nothing about which changes a
    candidate contains (``rc.24`` may contain *less* than ``rc.23``).
    """
    level = BumpLevel.PATCH
    reasons: list[str] = []
    for commit in commits:
        commit_level, reason = bump_for_commit(commit)
        reasons.append(reason)
        if commit_level.rank > level.rank:
            level = commit_level
    removals = list(api_removals)
    if removals:
        reasons.append("public API removed: " + ", ".join(sorted(removals)))
        level = BumpLevel.MAJOR

    published_set = set(published)
    tagged_set = set(tagged)
    known = [v for v in published_set | tagged_set if not v.is_prerelease]
    previous = max(known) if known else None
    proposed = previous.bump(level) if previous else initial
    if prerelease:
        if not prerelease.isalpha():
            raise ValueError("prerelease label must be alphabetic, e.g. 'rc'")
        numbers = [
            int(v.prerelease[1])
            for v in published_set | tagged_set
            if (v.major, v.minor, v.patch) == (proposed.major, proposed.minor, proposed.patch)
            and len(v.prerelease) == 2
            and v.prerelease[0] == prerelease
            and v.prerelease[1].isdigit()
        ]
        number = max(numbers, default=0) + 1
        proposed = SemVer(proposed.major, proposed.minor, proposed.patch, (prerelease, str(number)))
        reasons.append(
            f"{prerelease}.{number}: next candidate number for {proposed.major}."
            f"{proposed.minor}.{proposed.patch} (ordering only, not content)"
        )

    collisions = []
    if proposed in published_set:
        collisions.append(f"{proposed} already published in registry")
    if proposed in tagged_set:
        collisions.append(f"tag for {proposed} already exists")
    return VersionDecision(previous, proposed, level, tuple(reasons), tuple(collisions))
