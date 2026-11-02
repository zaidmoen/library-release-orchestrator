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
        return self.root / _SAFE.sub("_", spec.repository) / f"{_SAFE.sub('_', spec.head)}-{digest}.json"

    def ensure(self, spec: PullRequestSpec) -> PullRequestRef:
        path = self._record(spec)
        identifier = f"{spec.repository}#{path.stem.rsplit('-', 1)[-1]}"
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            return PullRequestRef(spec.repository, identifier, path.as_uri(), spec.head,
                                  data["base"], data["state"], created=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {**asdict(spec), "state": "open-draft" if spec.draft else "open"}
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return self.ensure(spec)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle, indent=2)
        return PullRequestRef(spec.repository, identifier, path.as_uri(), spec.head, spec.base,
                              record["state"], created=True)

    def list(self) -> list[dict[str, Any]]:
        if not self.root.is_dir():
            return []
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(self.root.rglob("*.json"))]
