"""Domain models.

Git history (``CommitInfo``), logical business changes (``ChangeUnit``) and dependency
evidence (``DependencyFinding``) are deliberately separate types: a change may span several
commits, and a successful cherry-pick says nothing about functional independence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


@dataclass(frozen=True)
class FileChange:
    status: str  # A, M, D, R (rename), C (copy), T (type change)
    path: str
    old_path: str | None = None


@dataclass(frozen=True)
class CommitInfo:
    sha: str
    parents: tuple[str, ...]
    author_name: str
    author_email: str
    subject: str
    body: str
    files: tuple[FileChange, ...]

    @property
    def short(self) -> str:
        return self.sha[:10]

    def to_dict(self) -> dict[str, Any]:
        return {
            "sha": self.sha,
            "author": f"{self.author_name} <{self.author_email}>",
            "subject": self.subject,
            "files": [f.path for f in self.files],
        }


@dataclass(frozen=True)
class ChangeUnit:
    """A logical business change (e.g. a Jira ticket) and the commits that implement it."""

    key: str
    commits: tuple[CommitInfo, ...]
    title: str = ""
    provenance: str = "commit-message"

    @property
    def files(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for commit in self.commits:
            for f in commit.files:
                seen.setdefault(f.path, None)
                if f.old_path:
                    seen.setdefault(f.old_path, None)
        return tuple(seen)

    @property
    def shas(self) -> tuple[str, ...]:
        return tuple(c.sha for c in self.commits)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "provenance": self.provenance,
            "commits": [c.to_dict() for c in self.commits],
        }


class Severity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    BLOCKER = "BLOCKER"


class FindingKind(StrEnum):
    SHARED_FILE = "SHARED_FILE"
    OVERLAPPING_HUNKS = "OVERLAPPING_HUNKS"
    GIT_CONFLICT = "GIT_CONFLICT"
    DIRECT_CODE_DEPENDENCY = "DIRECT_CODE_DEPENDENCY"
    MISSING_PREREQUISITE = "MISSING_PREREQUISITE"
    SHARED_SYMBOL_MODIFIED = "SHARED_SYMBOL_MODIFIED"
    PUBLIC_API_REMOVED = "PUBLIC_API_REMOVED"
    UNVERIFIED_FILE = "UNVERIFIED_FILE"
    PARSE_ERROR = "PARSE_ERROR"
    AMBIGUOUS_COMMIT = "AMBIGUOUS_COMMIT"
    MERGE_COMMIT_SKIPPED = "MERGE_COMMIT_SKIPPED"
    ALREADY_RELEASED = "ALREADY_RELEASED"


@dataclass(frozen=True)
class DependencyFinding:
    kind: FindingKind
    severity: Severity
    change: str
    message: str
    depends_on: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": str(self.kind),
            "severity": str(self.severity),
            "change": self.change,
            "depends_on": self.depends_on,
            "message": self.message,
            "evidence": self.evidence,
        }


class Verdict(StrEnum):
    INDEPENDENT = "INDEPENDENT"
    INDEPENDENT_WITH_RISKS = "INDEPENDENT_WITH_RISKS"
    DEPENDENT = "DEPENDENT"
    CONFLICT = "CONFLICT"
    UNKNOWN = "UNKNOWN"


class BumpLevel(StrEnum):
    PATCH = "patch"
    MINOR = "minor"
    MAJOR = "major"

    @property
    def rank(self) -> int:
        return {"patch": 0, "minor": 1, "major": 2}[self.value]
