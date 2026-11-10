"""Pull request port and the offline local provider.

Providers expose ``ensure`` (create-or-get, idempotent) and deliberately **no merge
operation**: merging is always a human decision.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class PullRequestSpec:
    repository: str  # logical name from config (library or consumer name)
    head: str
    base: str
    title: str
    body: str
    draft: bool = True


@dataclass(frozen=True)
class PullRequestRef:
    repository: str
    identifier: str
    url: str
    head: str
    base: str
    state: str
    created: bool  # False when an existing PR was found (idempotent re-run)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PullRequestProvider(Protocol):
    name: str

    def ensure(self, spec: PullRequestSpec) -> PullRequestRef: ...


class LocalPullRequestProvider:
    """Stores PRs as JSON records under ``<state_dir>/pull-requests``.

    Fully functional for demos and tests; nothing leaves the machine.
    """

    name = "local"

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _record(self, spec: PullRequestSpec) -> Path:
        digest = hashlib.sha256(f"{spec.repository}\0{spec.head}".encode()).hexdigest()[:8]
        return (
            self.root
            / _SAFE.sub("_", spec.repository)
            / f"{_SAFE.sub('_', spec.head)}-{digest}.json"
        )

    def ensure(self, spec: PullRequestSpec) -> PullRequestRef:
        path = self._record(spec)
        identifier = f"{spec.repository}#{path.stem.rsplit('-', 1)[-1]}"
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            return PullRequestRef(
                spec.repository,
                identifier,
                path.as_uri(),
                spec.head,
                data["base"],
                data["state"],
                created=False,
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {**asdict(spec), "state": "open-draft" if spec.draft else "open"}
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return self.ensure(spec)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle, indent=2)
        return PullRequestRef(
            spec.repository,
            identifier,
            path.as_uri(),
            spec.head,
            spec.base,
            record["state"],
            created=True,
        )

    def list(self) -> list[dict[str, Any]]:
        if not self.root.is_dir():
            return []
        return [
            json.loads(p.read_text(encoding="utf-8")) for p in sorted(self.root.rglob("*.json"))
        ]


class ExternalPullRequestProvider:
    """PRs created outside the engine, e.g. by Claude through the GitHub MCP server.

    ``ensure`` writes an auditable request (``<root>/<repo>/<head>-<id>.json``) and returns
    ``awaiting_external_creation`` until ``record`` stores the real PR number and URL.
    Re-running ``ensure`` after recording returns the recorded PR, so nothing is created
    twice. The engine never talks to GitHub here and has no merge capability.
    """

    name = "external"

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._local = LocalPullRequestProvider(root)

    def _path(self, repository: str, head: str) -> Path:
        spec = PullRequestSpec(repository, head, "", "", "")
        return self._local._record(spec)

    def ensure(self, spec: PullRequestSpec) -> PullRequestRef:
        path = self._path(spec.repository, spec.head)
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("number") is None and (data["base"], data["title"], data["body"]) != (
                spec.base,
                spec.title,
                spec.body,
            ):
                # Not created yet, so the request can still be corrected (e.g. new base).
                data |= {"base": spec.base, "title": spec.title, "body": spec.body}
                path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                **asdict(spec),
                "state": "awaiting_external_creation",
                "number": None,
                "url": None,
            }
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return PullRequestRef(
            spec.repository,
            str(data["number"]) if data.get("number") else "pending",
            data.get("url") or path.as_uri(),
            spec.head,
            data["base"],
            data["state"],
            created=False,
        )

    def record(
        self, repository: str, head: str, *, number: int, url: str, state: str
    ) -> dict[str, Any]:
        path = self._path(repository, head)
        if not path.is_file():
            raise FileNotFoundError(f"No PR request for {repository} {head}")
        if not re.match(r"^https://github\.com/[\w.-]+/[\w.-]+/pull/\d+$", url):
            raise ValueError("url must be a https://github.com/<owner>/<repo>/pull/<n> URL")
        if not url.endswith(f"/pull/{number}"):
            raise ValueError("url and number disagree")
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("number") not in (None, number):
            raise ValueError(f"A different PR (#{data['number']}) is already recorded")
        data |= {"number": number, "url": url, "state": state}
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return dict(data)

    def get(self, repository: str, head: str) -> dict[str, Any] | None:
        path = self._path(repository, head)
        if not path.is_file():
            return None
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return data

    def record_discovered(self, repository: str, head: str, *, number: int, url: str) -> None:
        """Record a PR found on the remote by ``pr_reconcile`` (never overwrites another)."""
        path = self._path(repository, head)
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("number") not in (None, number):
            raise ValueError(f"A different PR (#{data['number']}) is already recorded")
        data |= {"number": number, "url": url, "state": "exists (discovered via git)"}
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def requests(self) -> list[dict[str, Any]]:
        return self._local.list()
