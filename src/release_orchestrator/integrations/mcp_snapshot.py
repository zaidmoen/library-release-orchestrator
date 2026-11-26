"""MCP evidence snapshots: the bridge between Claude Code's MCP tools and the engine
(ADR-0017).

The Python engine can't call MCP tools; Claude Code can. So the Skill calls the
**read-only** GitHub and Jira MCP tools and hands their raw JSON output to
``release-orchestrator mcp-ingest``. This module:

* validates and normalises each payload into a small typed record (shapes verified
  against real ``list_branches`` / ``list_pull_requests`` / ``get_file_contents``
  responses; Jira follows the Jira REST v3 issue schema that the Atlassian tools return);
* stores it under ``<state_dir>/mcp`` with provenance (tool, ``fetched_at``);
* serves it to the providers below, which **prefer** fresh MCP evidence, fall back to
  REST/fixtures, and report ``unavailable`` / ``stale`` honestly.

Claude never interprets the data: the engine does, deterministically. Payload text is
untrusted: it's truncated, never executed, and never used as a path (file names are
hashes).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from release_orchestrator.errors import InvalidInputError
from release_orchestrator.integrations.github_status import PullSummary
from release_orchestrator.integrations.jira import Issue, IssueTracker

KINDS = (
    "github-pulls",
    "github-branches",
    "github-tags",
    "github-file",
    "github-code-search",
    "jira-issue",
)
DEFAULT_MAX_AGE_MINUTES = 30
_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$")
_KEY = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")
_SHA_IN_URI = re.compile(r"/sha/([0-9a-f]{40})/")
_MAX_TEXT = 512_000


@dataclass(frozen=True)
class Snapshot:
    kind: str
    repository: str | None
    key: str
    tool: str
    fetched_at: str
    data: Any
    source: str = "mcp"

    def age_minutes(self, now: datetime | None = None) -> float:
        when = datetime.fromisoformat(self.fetched_at.replace("Z", "+00:00"))
        return ((now or datetime.now(UTC)) - when).total_seconds() / 60

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- normalisers
def _clip(value: Any, limit: int = 2000) -> str:
    return str(value or "")[:limit]


def _pulls(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise InvalidInputError("github-pulls: expected a JSON list of pull requests")
    out = []
    for p in raw:
        head, base = p.get("head") or {}, p.get("base") or {}
        out.append(
            {
                "number": int(p["number"]),
                "title": _clip(p.get("title"), 300),
                "body": _clip(p.get("body"), 5000),
                "head_ref": _clip(head.get("ref"), 255),
                "head_sha": _clip(head.get("sha"), 40),
                "base_ref": _clip(base.get("ref"), 255),
                "state": _clip(p.get("state"), 20),
                "draft": bool(p.get("draft", False)),
                "merged": bool(p.get("merged") or p.get("merged_at")),
                "url": _clip(p.get("html_url"), 500),
                "author": _clip((p.get("user") or {}).get("login"), 100),
            }
        )
    return out


def _branches(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise InvalidInputError("github-branches: expected a JSON list of branches")
    return [
        {
            "name": _clip(b["name"], 255),
            "sha": _clip(b.get("sha") or (b.get("commit") or {}).get("sha"), 40),
            "protected": bool(b.get("protected", False)),
        }
        for b in raw
    ]


def _tags(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise InvalidInputError("github-tags: expected a JSON list of tags")
    return [
        {"name": _clip(t["name"], 255), "sha": _clip((t.get("commit") or {}).get("sha"), 40)}
        for t in raw
    ]


def _file(raw: Any) -> dict[str, Any]:
    """``get_file_contents`` result: ``{"content": [{type: text}, {type: resource, ...}]}``
    or plain text."""
    if isinstance(raw, str):
        return {"text": raw[:_MAX_TEXT], "blob_sha": None, "truncated": len(raw) > _MAX_TEXT}
    items = raw.get("content") if isinstance(raw, dict) else raw
    if isinstance(items, list):
        for item in items:
            resource = item.get("resource") if isinstance(item, dict) else None
            if isinstance(resource, dict) and isinstance(resource.get("text"), str):
                text = resource["text"]
                match = _SHA_IN_URI.search(str(resource.get("uri", "")))
                return {
                    "text": text[:_MAX_TEXT],
                    "blob_sha": None,
                    "commit_sha": match.group(1) if match else None,
                    "truncated": len(text) > _MAX_TEXT,
                }
    raise InvalidInputError("github-file: no text resource found in the payload")


def _code_search(raw: Any) -> list[dict[str, Any]]:
    items = raw.get("items") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        raise InvalidInputError("github-code-search: expected search results")
    out = []
    for item in items:
        repo = (item.get("repository") or {}).get("full_name") or item.get("repository")
        if isinstance(repo, str) and _REPO.match(repo):
            out.append({"repository": repo, "path": _clip(item.get("path"), 500)})
    return out


def _jira(raw: Any) -> dict[str, Any]:
    issue = raw.get("issues", [raw])[0] if isinstance(raw, dict) and "issues" in raw else raw
    if not isinstance(issue, dict) or not _KEY.match(str(issue.get("key", ""))):
        raise InvalidInputError("jira-issue: expected a Jira issue with a valid 'key'")
    fields = issue.get("fields") or {}
    blocked_by, blocks, relates = [], [], []
    for link in fields.get("issuelinks") or []:
        kind = (link.get("type") or {}).get("name", "")
        inward = (link.get("inwardIssue") or {}).get("key")
        outward = (link.get("outwardIssue") or {}).get("key")
        if kind == "Blocks" and inward and _KEY.match(inward):
            blocked_by.append(inward)  # "<this> is blocked by <inward>"
        elif kind == "Blocks" and outward and _KEY.match(outward):
            blocks.append(outward)
        elif (inward or outward) and _KEY.match(inward or outward or ""):
            relates.append(inward or outward)
    description = fields.get("description")
    if isinstance(description, dict):  # Atlassian Document Format: keep plain text only
        description = " ".join(re.findall(r'"text":\s*"([^"]*)"', json.dumps(description)))
    return {
        "key": issue["key"],
        "summary": _clip(fields.get("summary"), 300),
        "status": _clip((fields.get("status") or {}).get("name"), 60),
        "type": _clip((fields.get("issuetype") or {}).get("name"), 60),
        "blocked_by": sorted(set(blocked_by)),
        "blocks": sorted(set(blocks)),
        "relates": sorted(set(relates)),
        "components": [_clip(c.get("name"), 100) for c in fields.get("components") or []],
        "labels": [_clip(label, 100) for label in fields.get("labels") or []],
        "description": _clip(description, 2000),
    }


_NORMALISERS: dict[str, Callable[[Any], Any]] = {
    "github-pulls": _pulls,
    "github-branches": _branches,
    "github-tags": _tags,
    "github-file": _file,
    "github-code-search": _code_search,
    "jira-issue": _jira,
}


# --------------------------------------------------------------------------- store
class McpStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _path(self, kind: str, repository: str | None, key: str) -> Path:
        digest = hashlib.sha256(json.dumps([kind, repository, key]).encode()).hexdigest()
        return self.root / kind / f"{digest[:32]}.json"

    def ingest(
        self,
        kind: str,
        payload: Any,
        *,
        repository: str | None = None,
        key: str = "",
        tool: str = "",
        fetched_at: str | None = None,
    ) -> Snapshot:
        if kind not in KINDS:
            raise InvalidInputError(f"Unknown snapshot kind {kind!r}", details={"kinds": KINDS})
        if (
            kind.startswith("github")
            and kind != "github-code-search"
            and (not repository or not _REPO.match(repository))
        ):
            raise InvalidInputError(f"{kind} needs --repository owner/name")
        data = _NORMALISERS[kind](payload)
        if kind == "jira-issue":
            key = data["key"]
        stamp = fetched_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        snap = Snapshot(kind, repository, key, tool or kind, stamp, data)
        path = self._path(kind, repository, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(snap.to_dict(), handle)
        os.replace(tmp, path)
        return snap

    def get(self, kind: str, repository: str | None, key: str = "") -> Snapshot | None:
        path = self._path(kind, repository, key)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return Snapshot(**data)
        except (OSError, ValueError, TypeError):
            return None

    def all(self, kind: str) -> list[Snapshot]:
        out = []
        for path in sorted((self.root / kind).glob("*.json")):
            try:
                out.append(Snapshot(**json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, ValueError, TypeError):
                continue
        return out


# --------------------------------------------------------------------------- providers
class McpPullSource:
    """``PullSource`` backed by a ``github-pulls`` snapshot (state=all)."""

    def __init__(self, snap: Snapshot) -> None:
        self.snap = snap
        self.source = f"github-mcp (list_pull_requests, fetched_at {snap.fetched_at})"
        self._pulls = [
            PullSummary(
                number=p["number"],
                title=p["title"],
                body=p["body"],
                head_ref=p["head_ref"],
                base_ref=p["base_ref"],
                state=p["state"],
                draft=p["draft"],
                merged=p["merged"],
                head_sha=p["head_sha"],
                url=p["url"],
                author=p["author"],
                source="github-mcp",
            )
            for p in snap.data
        ]

    def get(self, number: int) -> PullSummary | None:
        return next((p for p in self._pulls if p.number == number), None)

    def list(self, *, state: str = "open", base: str | None = None) -> list[PullSummary]:
        return [
            p
            for p in sorted(self._pulls, key=lambda p: -p.number)
            if (state == "all" or p.state == state) and (base is None or p.base_ref == base)
        ]


class McpIssueTracker:
    """Live Jira evidence (``jira-issue`` snapshots) layered over a fallback tracker.

    Snapshots win for the issues they cover; others come from the fallback (fixture or
    nothing). Consumers listed in Jira aren't a standard field, so they come from the
    fallback only."""

    def __init__(self, store: McpStore, fallback: IssueTracker, *, max_age_minutes: int) -> None:
        self.fallback = fallback
        self.max_age = max_age_minutes
        self._snaps = {s.key: s for s in store.all("jira-issue")}

    def get(self, key: str) -> Issue | None:
        snap = self._snaps.get(key)
        if snap is None:
            return self.fallback.get(key)
        base = self.fallback.get(key)
        d = snap.data
        return Issue(
            key=d["key"],
            summary=d["summary"],
            consumers=base.consumers if base else (),
            status=d["status"],
            blocked_by=tuple(d["blocked_by"]),
            accessible=True,
            pull_requests=base.pull_requests if base else (),
        )

    def keys(self) -> list[str]:
        return sorted(set(self._snaps) | set(self.fallback.keys()))

    def fetched_at(self) -> datetime | None:
        """The *oldest* evidence in use, so staleness is never understated."""
        stamps = [
            datetime.fromisoformat(s.fetched_at.replace("Z", "+00:00"))
            for s in self._snaps.values()
        ]
        fallback = self.fallback.fetched_at()
        if fallback is not None:
            stamps.append(fallback)
        return min(stamps) if stamps else None

    def stale(self, now: datetime | None = None) -> list[str]:
        return sorted(k for k, s in self._snaps.items() if s.age_minutes(now) > self.max_age)
