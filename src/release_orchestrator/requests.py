"""Input & requirement resolver: normalise tickets, text and commits into change keys."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from release_orchestrator.errors import AmbiguousRequestError, InvalidInputError, NotFoundError
from release_orchestrator.gitutil.history import HistoryScan
from release_orchestrator.integrations.jira import IssueTracker

_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
_PR_RE = re.compile(r"(?:/pull/|\bPR\s*#?|#)(\d+)\b", re.IGNORECASE)
_BEFORE_RE = re.compile(r"\b(?:before|ahead of|prior to|without)\b", re.IGNORECASE)


@dataclass(frozen=True)
class ResolvedRequest:
    kind: str  # ticket | text | commits
    raw: str
    change_keys: tuple[str, ...]
    deferred_keys: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "raw": self.raw,
            "change_keys": list(self.change_keys),
            "deferred_keys": list(self.deferred_keys),
            "notes": list(self.notes),
        }


def resolve_ticket(key: str, *, key_pattern: str, tracker: IssueTracker) -> ResolvedRequest:
    key = key.strip()
    if not re.fullmatch(key_pattern, key):
        raise InvalidInputError(f"'{key}' does not look like a change key ({key_pattern})")
    notes: list[str] = []
    if tracker.keys() and tracker.get(key) is None:
        raise NotFoundError(f"Ticket {key} was not found in the issue tracker", details={"key": key})
    if not tracker.keys():
        notes.append("No issue tracker configured; ticket existence not verified")
    return ResolvedRequest("ticket", key, (key,), notes=tuple(notes))


def resolve_text(
    text: str, *, key_pattern: str, aliases: dict[str, str], tracker: IssueTracker
) -> ResolvedRequest:
    """Deterministic extraction of change keys from a natural-language request.

    "Release Change B before Change A" -> requested (FIN-204), deferred (FIN-198).
    Anything that cannot be mapped unambiguously raises ``AmbiguousRequestError`` so the
    caller (Claude, or a human) must clarify instead of guessing.
    """
    if _PR_RE.search(text) and not re.search(key_pattern, text):
        raise AmbiguousRequestError(
            "Pull request references need the GitHub provider to list their commits, which is "
            "not implemented yet; pass the ticket key or commit SHAs instead",
            details={"request": text},
        )
    mentions: list[tuple[int, str]] = []
    for match in re.finditer(key_pattern, text):
        mentions.append((match.start(), match.group(0)))
    lowered = text.lower()
    for alias, key in aliases.items():
        for match in re.finditer(rf"\b{re.escape(alias)}\b", lowered):
            mentions.append((match.start(), key))
    mentions.sort()
    if not mentions:
        raise AmbiguousRequestError(
            "No change key or known alias found in the request",
            details={"request": text, "known_aliases": sorted(aliases),
                     "known_tickets": tracker.keys()},
        )
    split = _BEFORE_RE.search(text)
    requested: list[str] = []
    deferred: list[str] = []
    for pos, key in mentions:
        bucket = deferred if split and pos > split.start() else requested
        if key not in requested and key not in deferred:
            bucket.append(key)
    if not requested:
        raise AmbiguousRequestError("The request names only changes to defer", details={"request": text})
    if tracker.keys():
        unknown = [k for k in requested + deferred if tracker.get(k) is None]
        if unknown:
            raise NotFoundError(f"Unknown ticket(s): {', '.join(unknown)}", details={"keys": unknown})
    notes = [f"Interpreted as: release {requested}"
             + (f" without {deferred}" if deferred else "")]
    return ResolvedRequest("text", text, tuple(requested), tuple(deferred), tuple(notes))


def resolve_commits(shas: list[str], scan: HistoryScan) -> ResolvedRequest:
    keys: list[str] = []
    matched: set[str] = set()
    for sha in shas:
        if not _SHA_RE.match(sha):
            raise InvalidInputError(f"'{sha}' is not a commit SHA")
        prefix = sha.lower()
        owner = [k for k, unit in scan.changes.items() if any(s.startswith(prefix) for s in unit.shas)]
        if not owner:
            raise NotFoundError(
                f"Commit {sha} is not a pending, mapped commit between "
                f"{scan.base_ref} and {scan.source_ref}",
                details={"commit": sha},
            )
        if len(owner) > 1:
            raise AmbiguousRequestError(f"Commit prefix {sha} matches several changes")
        matched.update(s for s in scan.changes[owner[0]].shas if s.startswith(prefix))
        if owner[0] not in keys:
            keys.append(owner[0])
    partial = [k for k in keys if set(scan.changes[k].shas) - matched]
    notes = [f"{k}: the whole logical change is released, not only the listed commits"
             for k in partial]
    return ResolvedRequest("commits", " ".join(shas), tuple(keys), notes=tuple(notes))
