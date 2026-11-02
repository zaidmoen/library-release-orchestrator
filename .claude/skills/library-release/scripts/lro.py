#!/usr/bin/env python3
"""Skill entry point for /library-release.

    python .claude/skills/library-release/scripts/lro.py intake "<$ARGUMENTS>"
    python .claude/skills/library-release/scripts/lro.py <cli args...>

``intake`` turns the raw skill argument string into structured JSON.
Anything else is forwarded to the ``release-orchestrator`` CLI with ``--json`` so the
Skill always reads one machine-readable envelope and a stable exit code.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _import_package() -> None:
    try:
        import release_orchestrator  # noqa: F401
    except ImportError:
        # Running from a checkout without `pip install -e .`: use the repo's src/.
        sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "src"))


def main(argv: list[str]) -> int:
    _import_package()
    from release_orchestrator.cli import main as cli_main
    from release_orchestrator.errors import OrchestratorError
    from release_orchestrator.skill_args import parse_skill_arguments

    if argv[:1] == ["intake"]:
        try:
            intake = parse_skill_arguments(" ".join(argv[1:]))
        except OrchestratorError as exc:
            print(json.dumps({"ok": False, "error": exc.to_dict()}, indent=2))
            return int(exc.exit_code)
        print(json.dumps({"ok": True, "intake": intake.to_dict()}, indent=2))
        return 0
    if "--json" not in argv:
        argv = ["--json", *argv]
    return cli_main(argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
