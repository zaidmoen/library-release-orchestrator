"""Issue tracker port and an offline fixture implementation.

Ticket text is untrusted: only the key, summary (for display) and the structured
``consumers`` list are consumed, and consumer names are later checked against config.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

import yaml

from release_orchestrator.errors import ConfigError

_CONSUMER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")


def _parse_time(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConfigError(f"fetched_at is not an ISO-8601 timestamp: {value!r}") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass(frozen=True)
class Issue:
    key: str
    summary: str
    consumers: tuple[str, ...] = ()
    status: str = ""
    blocked_by: tuple[str, ...] = ()  # issue links "is blocked by"
    accessible: bool = True  # False when the snapshot could not read the issue


class IssueTracker(Protocol):
    def get(self, key: str) -> Issue | None: ...

    def keys(self) -> list[str]: ...

    def fetched_at(self) -> datetime | None: ...


class NullIssueTracker:
    """Used when no tracker is configured: commit history is the only source of truth."""

    def get(self, key: str) -> Issue | None:
        return None

    def keys(self) -> list[str]:
        return []

    def fetched_at(self) -> datetime | None:
        return None


class FixtureIssueTracker:
    def __init__(self, path: Path) -> None:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise ConfigError(f"Cannot read issue fixture {path}: {exc}") from exc
        issues = raw.get("issues", {}) if isinstance(raw, dict) else {}
        self._fetched_at = _parse_time(raw.get("fetched_at")) if isinstance(raw, dict) else None
        if not isinstance(issues, dict):
            raise ConfigError("Issue fixture must contain an 'issues' mapping")
        self._issues: dict[str, Issue] = {}
        for key, data in issues.items():
            data = data or {}
            consumers = tuple(
                str(c) for c in data.get("consumers", []) or [] if _CONSUMER_RE.match(str(c))
            )
            self._issues[str(key)] = Issue(
                key=str(key),
                summary=str(data.get("summary", ""))[:200],
                consumers=consumers,
                status=str(data.get("status", "")),
                blocked_by=tuple(
                    str(b) for b in data.get("blocked_by", []) or [] if _KEY_RE.match(str(b))
                ),
                accessible=data.get("accessible", True) is not False,
            )

    def get(self, key: str) -> Issue | None:
        return self._issues.get(key)

    def fetched_at(self) -> datetime | None:
        return self._fetched_at

    def keys(self) -> list[str]:
        return sorted(self._issues)


# --------------------------------------------------------------------------- eligibility
@dataclass(frozen=True)
class EligibilityPolicy:
    """Issue-tracker rules for release eligibility (configured under ``issue_tracker``)."""

    release_statuses: tuple[str, ...] = ()  # empty = status not enforced
    resolved_statuses: tuple[str, ...] = ("Done", "Released", "Closed")
    max_age_minutes: int | None = None  # snapshot freshness


@dataclass
class IssueFinding:
    reason: str
    key: str
    message: str
    details: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "reason": self.reason,
            "key": self.key,
            "message": self.message,
            "details": self.details,
        }


def check_eligibility(
    tracker: IssueTracker,
    included: list[str],
    *,
    released: set[str],
    policy: EligibilityPolicy,
    now: datetime | None = None,
) -> list[IssueFinding]:
    """Deterministic issue-level release checks. Every finding blocks the plan.

    * snapshot older than ``max_age_minutes`` -> ``ISSUE_DATA_STALE``
    * issue unreadable -> ``ISSUE_INACCESSIBLE``
    * status not in ``release_statuses`` -> ``ISSUE_NOT_RELEASABLE``
    * blocker neither included, already released, nor resolved -> ``UNRESOLVED_BLOCKER``
    * cycle in "blocked by" links -> ``ISSUE_LINK_CYCLE``
    """
    findings: list[IssueFinding] = []
    if not tracker.keys():
        return findings
    fetched = tracker.fetched_at()
    if policy.max_age_minutes is not None:
        now = now or datetime.now(UTC)
        if fetched is None or now - fetched > timedelta(minutes=policy.max_age_minutes):
            stamp = fetched.isoformat() if fetched else "missing"
            findings.append(
                IssueFinding(
                    "ISSUE_DATA_STALE",
                    "*",
                    f"Issue snapshot is older than {policy.max_age_minutes} min "
                    f"(fetched_at={stamp}); refresh it before planning",
                )
            )
    for key in included:
        issue = tracker.get(key)
        if issue is None:
            continue  # existence is enforced at request resolution
        if not issue.accessible:
            findings.append(
                IssueFinding("ISSUE_INACCESSIBLE", key, f"{key} could not be read from the tracker")
            )
            continue
        if policy.release_statuses and issue.status not in policy.release_statuses:
            findings.append(
                IssueFinding(
                    "ISSUE_NOT_RELEASABLE",
                    key,
                    f"{key} is {issue.status!r}, not one of {list(policy.release_statuses)}",
                )
            )
        for blocker in issue.blocked_by:
            if blocker in included or blocker in released:
                continue
            other = tracker.get(blocker)
            if other is not None and other.status in policy.resolved_statuses:
                continue
            state = other.status if other else "not in snapshot"
            findings.append(
                IssueFinding(
                    "UNRESOLVED_BLOCKER",
                    key,
                    f"{key} is blocked by {blocker} ({state}), which is not in this release",
                    {"blocker": blocker},
                )
            )
    for cycle in _cycles(tracker, included):
        findings.append(
            IssueFinding(
                "ISSUE_LINK_CYCLE",
                cycle[0],
                f"'blocked by' links form a cycle: {' -> '.join(cycle)}",
                {"cycle": cycle},
            )
        )
    return findings


def _cycles(tracker: IssueTracker, roots: list[str]) -> list[list[str]]:
    found: list[list[str]] = []
    seen: set[frozenset[str]] = set()

    def walk(key: str, path: list[str]) -> None:
        issue = tracker.get(key)
        for nxt in issue.blocked_by if issue else ():
            if nxt in path:
                cycle = [*path[path.index(nxt) :], nxt]
                if frozenset(cycle) not in seen:
                    seen.add(frozenset(cycle))
                    found.append(cycle)
            elif len(path) < 50:
                walk(nxt, [*path, nxt])

    for root in sorted(roots):
        walk(root, [root])
    return found
