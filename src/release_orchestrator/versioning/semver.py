"""Strict Semantic Versioning 2.0.0 parsing and precedence (https://semver.org)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import total_ordering

from release_orchestrator.errors import InvalidInputError
from release_orchestrator.models import BumpLevel

_SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


@total_ordering
@dataclass(frozen=True)
class SemVer:
    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()
    build: tuple[str, ...] = ()

    @classmethod
    def parse(cls, text: str) -> SemVer:
        match = _SEMVER_RE.match(text.strip())
        if not match:
            raise InvalidInputError(f"'{text}' is not a valid semantic version")
        major, minor, patch, pre, build = match.groups()
        return cls(
            int(major),
            int(minor),
            int(patch),
            tuple(pre.split(".")) if pre else (),
            tuple(build.split(".")) if build else (),
        )

    @classmethod
    def try_parse(cls, text: str) -> SemVer | None:
        try:
            return cls.parse(text)
        except InvalidInputError:
            return None

    def __str__(self) -> str:
        text = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            text += "-" + ".".join(self.prerelease)
        if self.build:
            text += "+" + ".".join(self.build)
        return text

    @property
    def is_prerelease(self) -> bool:
        return bool(self.prerelease)

    def bump(self, level: BumpLevel) -> SemVer:
        if level is BumpLevel.MAJOR:
            # 1.0.0-rc.1 -> 1.0.0 is a valid "major" step for a pre-release of a major.
            if self.is_prerelease and self.minor == 0 and self.patch == 0:
                return SemVer(self.major, 0, 0)
            return SemVer(self.major + 1, 0, 0)
        if level is BumpLevel.MINOR:
            if self.is_prerelease and self.patch == 0:
                return SemVer(self.major, self.minor, 0)
            return SemVer(self.major, self.minor + 1, 0)
        if self.is_prerelease:
            return SemVer(self.major, self.minor, self.patch)
        return SemVer(self.major, self.minor, self.patch + 1)

    # Precedence ignores build metadata (SemVer §10-11).
    def _key(self) -> tuple[int, int, int, int, tuple[tuple[int, int | str], ...]]:
        pre = tuple((0, int(p)) if p.isdigit() else (1, p) for p in self.prerelease)
        return (self.major, self.minor, self.patch, 0 if self.prerelease else 1, pre)

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, SemVer):
            return NotImplemented
        return self._key() < other._key()

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SemVer):
            return NotImplemented
        return self._key() == other._key()

    def __hash__(self) -> int:
        return hash(self._key())
