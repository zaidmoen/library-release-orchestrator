"""Issue tracker port and an offline fixture implementation.

Ticket text is untrusted: only the key, summary (for display) and the structured
``consumers`` list are consumed, and consumer names are later checked against config.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import yaml

from release_orchestrator.errors import ConfigError

_CONSUMER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


@dataclass(frozen=True)
class Issue:
    key: str
    summary: str
    consumers: tuple[str, ...] = ()
    status: str = ""


class IssueTracker(Protocol):
    def get(self, key: str) -> Issue | None: ...

    def keys(self) -> list[str]: ...


class NullIssueTracker:
    """Used when no tracker is configured: commit history is the only source of truth."""

    def get(self, key: str) -> Issue | None:
        return None

    def keys(self) -> list[str]:
        return []


class FixtureIssueTracker:
    def __init__(self, path: Path) -> None:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise ConfigError(f"Cannot read issue fixture {path}: {exc}") from exc
        issues = raw.get("issues", {}) if isinstance(raw, dict) else {}
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
            )

    def get(self, key: str) -> Issue | None:
        return self._issues.get(key)

    def keys(self) -> list[str]:
        return sorted(self._issues)
