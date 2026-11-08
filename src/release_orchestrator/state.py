"""Run state machine and its persistence (``<state_dir>/runs/<plan_id>.json``)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from release_orchestrator.errors import InvalidTransitionError


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
        data = json.loads(path.read_text(encoding="utf-8"))
        return RunState(
            data["plan_id"],
            data["library"],
            data["version"],
            RunStatus(data["state"]),
            data["history"],
            data["steps"],
        )

    def save(self, run: RunState) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self._path(run.plan_id)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(run.to_dict(), indent=2), encoding="utf-8")
        os.replace(tmp, path)  # atomic on POSIX and Windows

    def create_or_load(self, plan_id: str, library: str, version: str) -> tuple[RunState, bool]:
        existing = self.load(plan_id)
        if existing is not None:
            return existing, False
        run = RunState(plan_id, library, version, RunStatus.ANALYZING)
        run.history.append({"at": _now(), "from": "", "to": "ANALYZING", "note": "run created"})
        return run, True
