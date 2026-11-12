"""Run state machine and its persistence (``<state_dir>/runs/<plan_id>.json``)."""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, TypeVar

from release_orchestrator.errors import ConcurrentModificationError, InvalidTransitionError

log = logging.getLogger(__name__)


class RunStatus(StrEnum):
    ANALYZING = "ANALYZING"
    PLAN_READY = "PLAN_READY"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    PREPARING = "PREPARING"
    VALIDATING = "VALIDATING"
    PR_READY = "PR_READY"
    PUBLISHED = "PUBLISHED"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


S = RunStatus
TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    S.ANALYZING: frozenset({S.PLAN_READY, S.BLOCKED, S.FAILED}),
    S.PLAN_READY: frozenset({S.APPROVAL_REQUIRED, S.PREPARING}),
    S.APPROVAL_REQUIRED: frozenset({S.PREPARING}),
    S.PREPARING: frozenset({S.VALIDATING, S.BLOCKED, S.FAILED}),
    S.VALIDATING: frozenset({S.PR_READY, S.BLOCKED, S.FAILED}),
    S.PR_READY: frozenset({S.PUBLISHED, S.BLOCKED, S.FAILED}),
    S.PUBLISHED: frozenset({S.COMPLETED, S.BLOCKED}),
    S.COMPLETED: frozenset(),
    S.BLOCKED: frozenset({S.ANALYZING, S.PREPARING, S.VALIDATING}),
    S.FAILED: frozenset({S.ANALYZING, S.PREPARING, S.VALIDATING, S.PUBLISHED}),
}
# States at or after which a step's result already exists (used for idempotency).
PREPARED_STATES = frozenset({S.VALIDATING, S.PR_READY, S.PUBLISHED, S.COMPLETED})
VALIDATED_STATES = frozenset({S.PR_READY, S.PUBLISHED, S.COMPLETED})
PUBLISHED_STATES = frozenset({S.PUBLISHED, S.COMPLETED})


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class RunState:
    plan_id: str
    library: str
    version: str
    state: RunStatus
    history: list[dict[str, str]] = field(default_factory=list)
    steps: dict[str, Any] = field(default_factory=dict)
    revision: int = 0  # optimistic concurrency: the revision this copy was loaded at

    def transition(self, target: RunStatus, note: str = "") -> None:
        if target not in TRANSITIONS[self.state]:
            raise InvalidTransitionError(
                f"Cannot move from {self.state} to {target}",
                details={
                    "from": str(self.state),
                    "to": str(target),
                    "allowed": sorted(str(s) for s in TRANSITIONS[self.state]),
                },
            )
        log.info(
            "state_transition",
            extra={
                "plan_id": self.plan_id,
                "from_state": str(self.state),
                "to_state": str(target),
                "note": note,
                "version": self.version,
            },
        )
        self.history.append(
            {"at": _now(), "from": str(self.state), "to": str(target), "note": note}
        )
        self.state = target

    def record(self, step: str, result: Any) -> None:
        self.steps[step] = {"at": _now(), "result": result}

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "library": self.library,
            "version": self.version,
            "state": str(self.state),
            "history": self.history,
            "steps": self.steps,
            "revision": self.revision,
        }


class RunStore:
    def __init__(self, state_dir: Path) -> None:
        self.dir = Path(state_dir) / "runs"

    def _path(self, plan_id: str) -> Path:
        if not plan_id.isalnum():
            raise ValueError("invalid plan id")
        return self.dir / f"{plan_id}.json"

    def load(self, plan_id: str) -> RunState | None:
        path = self._path(plan_id)
        if not path.is_file():
            return None
        data = json.loads(_read_with_retry(path))
        return RunState(
            data["plan_id"],
            data["library"],
            data["version"],
            RunStatus(data["state"]),
            data["history"],
            data["steps"],
            int(data.get("revision", 0)),
        )

    def save(self, run: RunState) -> None:
        """Compare-and-swap: write only if nobody saved since ``run`` was loaded.

        Two processes working on the same release can't silently overwrite each other's
        transitions: the loser gets ``ConcurrentModificationError`` and must reload. The
        compare and the write happen under an ``O_EXCL`` lock file (one filesystem; a
        shared store would use a database row version instead).

        Every wait is bounded. Only contention is retried; a real permission problem is
        raised as itself, never disguised as "locked".
        """
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self._path(run.plan_id)
        lock = path.with_suffix(".lock")
        self._acquire(lock, run.plan_id)
        tmp = path.with_suffix(".json.tmp")
        try:
            stored = self.load(run.plan_id)
            current = stored.revision if stored is not None else 0
            if current != run.revision:
                raise ConcurrentModificationError(
                    f"Run {run.plan_id} changed since it was loaded (revision {run.revision} "
                    f"-> {current}); reload and retry",
                    details={"loaded": run.revision, "stored": current},
                )
            data = {**run.to_dict(), "revision": current + 1}
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            _transient_retry(lambda: os.replace(tmp, path))
            run.revision = current + 1  # only after the write is durable
        finally:
            _quiet_unlink(tmp)
            _quiet_unlink(lock)

    @staticmethod
    def _acquire(lock: Path, plan_id: str, attempts: int = 200) -> None:
        last_permission: PermissionError | None = None
        for _ in range(attempts):
            try:
                os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                return
            except FileExistsError:
                last_permission = None
            except PermissionError as exc:
                if not _WINDOWS:
                    raise  # POSIX: a real permission problem, never contention
                # Windows reports a lock that is being deleted ("delete pending") as
                # PermissionError. Treat it as contention, but remember it: if it persists
                # while no lock file is visible, it is a genuine permission failure.
                last_permission = exc
            time.sleep(0.01)
        if last_permission is not None and not lock.exists():
            raise last_permission
        raise ConcurrentModificationError(
            f"Run {plan_id} is locked by another process ({lock.name}); retry, or remove the "
            "stale lock if no other process is running",
            details={"lock": str(lock)},
        )

    def create_or_load(self, plan_id: str, library: str, version: str) -> tuple[RunState, bool]:
        existing = self.load(plan_id)
        if existing is not None:
            return existing, False
        run = RunState(plan_id, library, version, RunStatus.ANALYZING)
        run.history.append({"at": _now(), "from": "", "to": "ANALYZING", "note": "run created"})
        return run, True


_WINDOWS = os.name == "nt"
T = TypeVar("T")


def _transient_retry(operation: Callable[[], T], attempts: int = 100) -> T:
    """Retry Windows sharing violations (another process briefly has the file open).

    On POSIX a PermissionError is never transient and is raised immediately. Bounded:
    after ``attempts`` the original error is raised.
    """
    for attempt in range(attempts):
        try:
            return operation()
        except PermissionError:
            if not _WINDOWS or attempt == attempts - 1:
                raise
            time.sleep(0.01)
    raise AssertionError("unreachable")


def _read_with_retry(path: Path) -> str:
    return _transient_retry(lambda: path.read_text(encoding="utf-8"))


def _quiet_unlink(path: Path) -> None:
    """Best-effort cleanup that never masks the exception being handled."""
    try:
        _transient_retry(lambda: path.unlink(missing_ok=True))
    except OSError:
        log.warning("could not remove %s; remove it manually if no process is running", path)
