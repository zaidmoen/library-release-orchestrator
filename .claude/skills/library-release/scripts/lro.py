#!/usr/bin/env python3
"""Skill entry point for /library-release.

    python .claude/skills/library-release/scripts/lro.py intake "<$ARGUMENTS>"
    python .claude/skills/library-release/scripts/lro.py <cli args...>

``intake`` turns the raw skill argument string into structured JSON.
Anything else is forwarded to the ``release-orchestrator`` CLI with ``--json`` so the
Skill always reads one machine-readable envelope and a stable exit code.

The Skill and the engine are versioned together: this script always runs the engine from
**its own repository** (``<repo>/src``). It never uses a ``release_orchestrator`` installed
elsewhere, for example an editable install of another, older checkout, which would make
the Skill silently run different release logic.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_SRC = Path(__file__).resolve().parents[4] / "src"


def _revision() -> str:
    """The engine revision for audit: the verified pinned install, else this checkout."""
    root = REPO_SRC.parent
    marker = root.parent / "engine.json"  # <LRO_HOME>/engines/<sha>/engine.json
    try:
        sha = json.loads(marker.read_text(encoding="utf-8"))["sha"]
        if sha == root.parent.name:
            return f"{sha} (pinned install)"
    except (OSError, ValueError, KeyError):
        pass
    try:
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return f"{head} (development checkout)"


def _import_package() -> Path:
    if not (REPO_SRC / "release_orchestrator" / "__init__.py").is_file():
        raise SystemExit(f"Engine not found at {REPO_SRC}; the Skill must live in its repository")
    sys.path.insert(0, str(REPO_SRC))
    for name in [
        m
        for m in sys.modules
        if m == "release_orchestrator" or m.startswith("release_orchestrator.")
    ]:
        del sys.modules[name]  # never reuse a copy imported from elsewhere
    import release_orchestrator

    loaded = Path(release_orchestrator.__file__).resolve().parent
    if loaded != (REPO_SRC / "release_orchestrator").resolve():
        raise SystemExit(f"Refusing to run engine from {loaded}; expected {REPO_SRC}")
    return loaded


def main(argv: list[str]) -> int:
    engine = _import_package()
    os.environ.setdefault("LRO_ENGINE_REVISION", _revision())
    from release_orchestrator import __version__
    from release_orchestrator.cli import main as cli_main
    from release_orchestrator.errors import OrchestratorError
    from release_orchestrator.skill_args import parse_skill_arguments

    if argv[:1] == ["intake"]:
        try:
            intake = parse_skill_arguments(" ".join(argv[1:]))
        except OrchestratorError as exc:
            print(json.dumps({"ok": False, "error": exc.to_dict()}, indent=2))
            return int(exc.exit_code)
        print(
            json.dumps(
                {
                    "ok": True,
                    "intake": intake.to_dict(),
                    "engine": {
                        "path": str(engine),
                        "version": __version__,
                        "revision": os.environ["LRO_ENGINE_REVISION"],
                    },
                },
                indent=2,
            )
        )
        return 0
    if "--json" not in argv:
        argv = ["--json", *argv]
    return cli_main(argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
