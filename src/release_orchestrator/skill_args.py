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
_BOOL_FLAGS = {"--dry-run": "dry_run", "--plan-only": "plan_only"}
_VALUE_FLAGS = {"--base": "base", "--source": "source", "--config": "config"}


@dataclass
class SkillIntake:
    mode: str  # ticket | commits | text | ask
    tickets: list[str] = field(default_factory=list)
    commits: list[str] = field(default_factory=list)
    text: str | None = None
    dry_run: bool = False
    plan_only: bool = False
    base: str | None = None
    source: str | None = None
    config: str | None = None

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
        return data


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
        else:
            words.append(token)
        i += 1

    if not words:
        return intake
    if all(_KEY.match(w) for w in words):
        intake.mode, intake.tickets = "ticket", words
    elif all(_SHA.match(w) for w in words):
        intake.mode, intake.commits = "commits", words
    else:
        intake.mode, intake.text = "text", " ".join(words)
    return intake
