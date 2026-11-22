"""Content-addressed disk cache for **immutable** analysis results only (ADR-0016).

What may be cached is decided by the data, not by time:

* allowed namespaces are facts derived from Git objects addressed by full SHAs (commit
  metadata, patch-ids, per-commit AST effects, cherry-pick probe outcomes). The same
  inputs always give the same output, so an entry never goes stale;
* every key includes a fingerprint of the analysis source code (and the schema), so a code
  change invalidates old entries even without a version bump;
* anything mutable (ref -> SHA, remote tags, PR reviews, CI checks, Jira, registry,
  environments) is **refused** by ``put``: it must be read live on every run.

Entries are JSON files written atomically. A corrupted or unreadable entry is ignored and
recomputed. ``LRO_NO_CACHE=1`` / ``--no-cache`` disables the cache entirely.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any

from release_orchestrator import __version__

SCHEMA = "1"
# Modules whose code determines the cached facts. Their source hash is part of every key,
# so changing analysis logic invalidates old entries even without a version bump.
_ANALYSIS_SOURCES = (
    "cache.py",
    "models.py",
    "gitutil/repo.py",
    "gitutil/history.py",
    "analysis/dependency.py",
    "analysis/python_symbols.py",
)
_FINGERPRINT: str | None = None


def code_fingerprint() -> str:
    """SHA-256 of the analysis sources (computed once per process, ~100 KB of reads)."""
    global _FINGERPRINT
    if _FINGERPRINT is None:
        digest = hashlib.sha256(__version__.encode())
        package = Path(__file__).resolve().parent
        for rel in _ANALYSIS_SOURCES:
            digest.update(rel.encode())
            digest.update((package / rel).read_bytes())
        _FINGERPRINT = digest.hexdigest()
    return _FINGERPRINT


IMMUTABLE_NAMESPACES = frozenset({"commit", "patch_id", "effects", "probe", "git"})


class MutableCacheError(ValueError):
    """Raised when code tries to cache mutable state."""


class AnalysisCache:
    def __init__(self, root: Path | None, *, enabled: bool = True, max_entries: int = 50_000):
        self.root = root
        self.enabled = enabled and root is not None
        self.max_entries = max_entries
        self.hits = self.misses = self.writes = self.errors = 0
        self._count: int | None = None

    @classmethod
    def disabled(cls) -> AnalysisCache:
        return cls(None, enabled=False)

    @staticmethod
    def _check(namespace: str) -> None:
        if namespace not in IMMUTABLE_NAMESPACES:
            raise MutableCacheError(f"namespace {namespace!r} is not an immutable fact")

    def _path(self, namespace: str, parts: tuple[str, ...]) -> Path:
        digest = hashlib.sha256(
            json.dumps([namespace, code_fingerprint(), SCHEMA, *parts]).encode("utf-8")
        ).hexdigest()
        assert self.root is not None  # noqa: S101
        return self.root / f"v{SCHEMA}" / namespace / digest[:2] / f"{digest}.json"

    def get(self, namespace: str, *parts: str) -> Any | None:
        self._check(namespace)  # refused even when disabled
        if not self.enabled:
            return None
        path = self._path(namespace, parts)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.misses += 1
            return None
        except (OSError, ValueError):
            self.errors += 1  # corrupted entry: ignore, recompute, overwrite
            return None
        if not isinstance(payload, dict) or payload.get("parts") != list(parts):
            self.errors += 1
            return None
        self.hits += 1
        return payload.get("value")

    def put(self, namespace: str, value: Any, *parts: str) -> None:
        self._check(namespace)
        if not self.enabled:
            return
        path = self._path(namespace, parts)
        if self._count is None:
            self._count = (
                sum(1 for _ in path.parents[2].rglob("*.json")) if path.parents[2].exists() else 0
            )
        if self._count >= self.max_entries:
            return  # full: keep working uncached rather than evicting unpredictably
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"parts": list(parts), "value": value}, handle)
            os.replace(tmp, path)
            self.writes += 1
            self._count += 1
        except OSError:
            self.errors += 1
            with suppress(OSError, UnboundLocalError):
                os.unlink(tmp)

    def clear(self) -> int:
        if self.root is None or not (self.root / f"v{SCHEMA}").exists():
            return 0
        removed = 0
        for item in (self.root / f"v{SCHEMA}").rglob("*.json"):
            with suppress(OSError):
                item.unlink()
                removed += 1
        self._count = 0
        return removed

    def stats(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "hits": self.hits,
            "misses": self.misses,
            "writes": self.writes,
            "errors": self.errors,
        }


_ACTIVE = AnalysisCache.disabled()


def active() -> AnalysisCache:
    return _ACTIVE


def configure(cache: AnalysisCache) -> AnalysisCache:
    """Install the process-wide cache (CLI start-up). Returns the previous one."""
    global _ACTIVE
    previous, _ACTIVE = _ACTIVE, cache
    return previous


def for_state_dir(state_dir: Path, *, enabled: bool = True) -> AnalysisCache:
    enabled = enabled and os.environ.get("LRO_NO_CACHE", "0") != "1"
    return AnalysisCache(state_dir / "cache", enabled=enabled)
