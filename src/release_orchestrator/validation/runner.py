"""Validation engine: runs allowlisted commands from the trusted config.

Commands come only from ``release-orchestrator.yaml``; the plan file merely lists them
for review and is checked against config before anything runs. A command that is not
executed is never reported as passed.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from release_orchestrator.config import CommandSpec

_SECRET_ENV = re.compile(r"(TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|PRIVATE_KEY|CREDENTIAL)", re.I)
_TAIL_LINES = 40


@dataclass
class CommandResult:
    scope: str
    name: str
    argv: list[str]
    status: str  # PASSED | FAILED | TIMED_OUT | ERROR
    exit_code: int | None
    duration_seconds: float
    output_tail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "name": self.name,
            "argv": self.argv,
            "status": self.status,
            "exit_code": self.exit_code,
            "duration_seconds": self.duration_seconds,
            "output_tail": self.output_tail,
        }


@dataclass
class ValidationReport:
    scope: str
    results: list[CommandResult] = field(default_factory=list)

    @property
    def status(self) -> str:
        if not self.results:
            return "NOT_CONFIGURED"
        return "PASSED" if all(r.status == "PASSED" for r in self.results) else "FAILED"

    @property
    def passed(self) -> bool:
        return self.status == "PASSED"

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "status": self.status,
            "results": [r.to_dict() for r in self.results],
        }


def _substitute(value: str, mapping: dict[str, str]) -> str:
    for key, replacement in mapping.items():
        value = value.replace("{" + key + "}", replacement)
    return value


def _clean_env() -> dict[str, str]:
    """Inherit the environment minus anything that looks like a credential."""
    return {k: v for k, v in os.environ.items() if not _SECRET_ENV.search(k)}


def run_validation(
    specs: tuple[CommandSpec, ...], *, scope: str, cwd: Path, library_path: Path
) -> ValidationReport:
    report = ValidationReport(scope=scope)
    mapping = {"python": sys.executable, "library_path": str(library_path)}
    for spec in specs:
        argv = [_substitute(a, mapping) for a in spec.argv]
        env = _clean_env() | {"PYTHONDONTWRITEBYTECODE": "1"}
        env |= {k: _substitute(v, mapping) for k, v in spec.env}
        started = time.monotonic()
        try:
            proc = subprocess.run(
                argv,
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=spec.timeout_seconds,
                check=False,
            )
            status = "PASSED" if proc.returncode == 0 else "FAILED"
            output, code = (proc.stdout + proc.stderr), proc.returncode
        except subprocess.TimeoutExpired as exc:
            status, code = "TIMED_OUT", None
            raw = exc.stdout or b""
            partial = raw.decode(errors="replace") if isinstance(raw, bytes) else raw
            output = f"timed out after {spec.timeout_seconds}s\n{partial}"
        except OSError as exc:
            status, code, output = "ERROR", None, f"could not start command: {exc}"
        tail = "\n".join(str(output).strip().splitlines()[-_TAIL_LINES:])
        report.results.append(
            CommandResult(
                scope,
                spec.name,
                list(spec.argv),
                status,
                code,
                round(time.monotonic() - started, 2),
                tail,
            )
        )
    return report
