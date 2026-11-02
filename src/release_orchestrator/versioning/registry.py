"""Package registry port and a local, immutable implementation (ADR-0005)."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from release_orchestrator.errors import ImmutableVersionError, NotFoundError
from release_orchestrator.versioning.semver import SemVer

_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


@dataclass(frozen=True)
class PublishedArtifact:
    name: str
    version: str
    sha256: str
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "version": self.version, "sha256": self.sha256,
                "metadata": self.metadata}


class PackageRegistry(Protocol):
    def list_versions(self, name: str) -> list[SemVer]: ...

    def get(self, name: str, version: SemVer) -> PublishedArtifact | None: ...

    def fetch(self, name: str, version: SemVer) -> bytes: ...

    def publish(
        self, name: str, version: SemVer, payload: bytes, metadata: dict[str, Any]
    ) -> tuple[PublishedArtifact, bool]:
        """Create-if-absent. Returns (artifact, created). Must never overwrite."""
        ...


class LocalRegistry:
    """Directory-backed registry. ``O_EXCL`` makes publication atomic on one filesystem.

    It does **not** provide cross-machine coordination; production registries must give
    the same create-if-absent guarantee (PyPI/Artifactory/CodeArtifact do).
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _dir(self, name: str) -> Path:
        if not _SAFE_NAME.match(name):
            raise NotFoundError(f"Invalid package name '{name}'")
        return self.root / name.lower()

    def list_versions(self, name: str) -> list[SemVer]:
        directory = self._dir(name)
        if not directory.is_dir():
            return []
        versions = []
        for record in directory.glob("*.json"):
            parsed = SemVer.try_parse(record.stem)
            if parsed is not None:
                versions.append(parsed)
        return sorted(versions)

    def get(self, name: str, version: SemVer) -> PublishedArtifact | None:
        record = self._dir(name) / f"{version}.json"
        if not record.is_file():
            return None
        try:
            data = json.loads(record.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            # A record exists but is mid-write or corrupt: the version is still taken.
            return PublishedArtifact(name, str(version), "", {"state": "incomplete"})
        return PublishedArtifact(name, str(version), data["sha256"], data.get("metadata", {}))

    def fetch(self, name: str, version: SemVer) -> bytes:
        artifact = self.get(name, version)
        if artifact is None:
            raise NotFoundError(f"{name} {version} is not published")
        payload = (self._dir(name) / f"{version}.tar").read_bytes()
        if hashlib.sha256(payload).hexdigest() != artifact.sha256:
            raise ImmutableVersionError(
                f"Digest mismatch for {name} {version}: artifact was modified after publish"
            )
        return payload

    def publish(
        self, name: str, version: SemVer, payload: bytes, metadata: dict[str, Any]
    ) -> tuple[PublishedArtifact, bool]:
        directory = self._dir(name)
        directory.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(payload).hexdigest()
        record = directory / f"{version}.json"
        try:
            fd = os.open(record, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            existing = self.get(name, version)
            if existing is not None and existing.sha256 == digest:
                return existing, False
            raise ImmutableVersionError(
                f"{name} {version} is already published with different content",
                details={"existing_sha256": existing.sha256 if existing else None,
                         "attempted_sha256": digest},
            ) from None
        # We own the version now. Write payload first, then the record body.
        (directory / f"{version}.tar").write_bytes(payload)
        body = json.dumps({"sha256": digest, "metadata": metadata}, sort_keys=True, indent=2)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
        return PublishedArtifact(name, str(version), digest, metadata), True
