"""Deterministic parsing of ``/library-release`` arguments.

Claude Code passes skill arguments as one raw string (``$ARGUMENTS``); it does not parse
CLI-style flags. This module turns that string into a structured intake the Skill acts on.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import asdict, dataclass, field
from typing import Any

from release_orchestrator.errors import InvalidInputError

_KEY = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")
_SHA = re.compile(r"^[0-9a-f]{7,40}$")
_PR = re.compile(r"^(#\d{1,9}|https://[^/\s]+/[^/\s]+/[^/\s]+/pull/\d{1,9}/?)$")
_BOOL_FLAGS = {"--dry-run": "dry_run", "--plan-only": "plan_only"}
_VALUE_FLAGS = {
    "--base": "base",
    "--source": "source",
    "--config": "config",
    "--mode": "release_mode",
    "--candidate": "candidate",
    "--order": "order",
    "--pr": "pr",
    "--repository": "repository",
}
RELEASE_MODES = ("release-candidate", "release", "reorder-releases")


@dataclass
class SkillIntake:
    mode: str  # ticket | pr | commits | text | ask
    tickets: list[str] = field(default_factory=list)
    release_mode: str | None = None  # release-candidate | release | reorder-releases
    candidate: str | None = None
    order: str | None = None  # comma-separated candidates
    commits: list[str] = field(default_factory=list)
    text: str | None = None
    dry_run: bool = False
    plan_only: bool = False
    base: str | None = None
    source: str | None = None
    config: str | None = None
    pr: str | None = None  # PR number/#number/URL (ADR-0015: one reference is enough)
    repository: str | None = None  # owner/name override when discovery can't tell

    def cli_request_args(self) -> list[str]:
        args: list[str] = []
        if self.mode == "ticket":
            for t in self.tickets:
                args += ["--ticket", t]
        elif self.mode == "commits":
            for c in self.commits:
                args += ["--commit", c]
        elif self.mode == "text" and self.text:
            args += ["--request", self.text]
        if self.base:
            args += ["--base", self.base]
        if self.source:
            args += ["--source", self.source]
        return args

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["max_step"] = "plan" if self.plan_only else ("dry-run" if self.dry_run else "full")
        data["plan_args"] = self.cli_request_args()
        data["mode_args"] = self.mode_args()
        data["resolve_args"] = self.resolve_args()
        data["global_args"] = ["--repository", self.repository] if self.repository else []
        return data

    def resolve_args(self) -> list[str]:
        """Arguments for ``resolve-change`` (PR and/or the single ticket given)."""
        args: list[str] = []
        if self.pr:
            args += ["--pr", self.pr]
        if len(self.tickets) == 1:
            args += ["--ticket", self.tickets[0]]
        return args

    def mode_args(self) -> list[str]:
        """Arguments for ``release-mode options|preview`` when a release mode was given."""
        if not self.release_mode:
            return []
        args = ["--mode", self.release_mode]
        args += [a for t in self.tickets for a in ("--ticket", t)]
        if self.candidate:
            args += ["--candidate", self.candidate]
        if self.order:
            args += ["--order", self.order]
        return args


def parse_skill_arguments(raw: str) -> SkillIntake:
    try:
        tokens = shlex.split(raw or "")
    except ValueError as exc:
        raise InvalidInputError(f"Cannot parse arguments: {exc}") from exc
    intake = SkillIntake(mode="ask")
    words: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in _BOOL_FLAGS:
            setattr(intake, _BOOL_FLAGS[token], True)
        elif token in _VALUE_FLAGS:
            if i + 1 >= len(tokens):
                raise InvalidInputError(f"{token} needs a value")
            setattr(intake, _VALUE_FLAGS[token], tokens[i + 1])
            i += 1
        elif token.startswith("--"):
            raise InvalidInputError(
                f"Unknown option {token}", details={"known": sorted([*_BOOL_FLAGS, *_VALUE_FLAGS])}
            )
        elif _PR.match(token) and intake.pr is None:
            intake.pr = token
        else:
            words.append(token)
        i += 1

    if intake.release_mode is not None and intake.release_mode not in RELEASE_MODES:
        raise InvalidInputError(
            f"Unknown release mode {intake.release_mode!r}", details={"modes": RELEASE_MODES}
        )
    if intake.pr is not None and not _PR.match(intake.pr) and not intake.pr.isdigit():
        raise InvalidInputError(f"--pr must be a number, #number or PR URL, got {intake.pr!r}")
    if not words:
        if intake.pr is not None:
            intake.mode = "pr"
        return intake
    if all(_KEY.match(w) for w in words):
        intake.mode, intake.tickets = "ticket", words
    elif all(_SHA.match(w) for w in words):
        intake.mode, intake.commits = "commits", words
    else:
        intake.mode, intake.text = "text", " ".join(words)
    return intake
