"""Phase 1 benchmark: baseline Skill flow vs one ``assess`` call (ADR-0016).

Usage (from a lab library repository, e.g. ``demo/harri-release-lab-library``)::

    python scripts/bench_phase1.py --engine-src <src dir> --flow baseline|assess [--repeat N]

Each step runs in a **fresh process** (like the Skill), wrapped so that every Git
subprocess is counted and timed, independent of the engine version being measured:

* ``e2e_seconds``: wall clock of the process (Python start-up included: what the Skill
  waits for);
* ``engine_seconds``: in-process time minus Git subprocess time;
* ``git_seconds`` / ``git_calls``: all ``git`` subprocesses (including ``ls-remote``);
* network: the lab has no remote, so it is 0 here; ``assess --json`` reports it per run.

Only read-only commands are run. Nothing is pushed, published or changed.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

CHILD = r"""
import json, subprocess, sys, time, io, contextlib
sys.path.insert(0, sys.argv[1])
calls = {"n": 0, "s": 0.0}
_run, _popen_init = subprocess.run, subprocess.Popen.__init__
def run(*a, **k):
    cmd = a[0] if a else k.get("args")
    is_git = isinstance(cmd, (list, tuple)) and cmd and str(cmd[0]).endswith("git")
    t = time.perf_counter()
    try:
        return _run(*a, **k)
    finally:
        if is_git:
            calls["n"] += 1; calls["s"] += time.perf_counter() - t
def popen_init(self, *a, **k):
    cmd = a[0] if a else k.get("args")
    if isinstance(cmd, (list, tuple)) and cmd and str(cmd[0]).endswith("git") and "cat-file" in cmd:
        calls["n"] += 1
    return _popen_init(self, *a, **k)
subprocess.run = run
subprocess.Popen.__init__ = popen_init
from release_orchestrator.cli import main
t = time.perf_counter(); buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    code = main(sys.argv[2:])
total = time.perf_counter() - t
out = buf.getvalue()
print(json.dumps({"exit": code, "in_process": total, "git_calls": calls["n"],
                  "git_seconds": calls["s"], "bytes_out": len(out.encode())}))
"""

BASELINE = [
    ["--json", "--repository", "zaidmoen/zaidlibrary", "context"],
    ["--json", "resolve-change", "--pr", "15"],
    ["--json", "release-graph", "--ticket", "FIN-211"],
    ["--json", "release-mode", "discover"],
    ["--json", "release-mode", "options", "--mode", "release-candidate", "--ticket", "FIN-211"],
    [
        "--json",
        "release-mode",
        "preview",
        "--mode",
        "release-candidate",
        "--ticket",
        "FIN-211",
        "--choice",
        "with-prerequisites",
        "--services",
        "all",
    ],
    [
        "--json",
        "release-mode",
        "preview",
        "--mode",
        "release-candidate",
        "--ticket",
        "FIN-211",
        "--choice",
        "with-prerequisites",
        "--services",
        "all",
        "--out",
        "bench-preview.json",
    ],
    [
        "--json",
        "--repository",
        "zaidmoen/zaidlibrary",
        "release-report",
        "--preview",
        "bench-preview.json",
        "--pr",
        "15",
    ],
]
ASSESS = [["--json", "--repository", "zaidmoen/zaidlibrary", "assess", "--pr", "15"]]


def run_step(src: str, args: list[str], env: dict[str, str]) -> dict[str, float]:
    start = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, "-c", CHILD, src, *args], capture_output=True, text=True, env=env
    )
    e2e = time.perf_counter() - start
    if proc.returncode != 0 or not proc.stdout.strip():
        raise SystemExit(f"step failed: {args}\n{proc.stderr[-2000:]}")
    data = json.loads(proc.stdout.strip().splitlines()[-1])
    return {
        "e2e_seconds": round(e2e, 2),
        "engine_seconds": round(data["in_process"] - data["git_seconds"], 2),
        "git_seconds": round(data["git_seconds"], 2),
        "git_calls": data["git_calls"],
        "output_bytes": data["bytes_out"],
        "exit": data["exit"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine-src", required=True)
    parser.add_argument("--flow", choices=["baseline", "assess"], required=True)
    parser.add_argument("--label", default="")
    args = parser.parse_args()
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    env.pop("LRO_CONFIG", None)
    steps = BASELINE if args.flow == "baseline" else ASSESS
    results = [{"step": " ".join(s[1:]), **run_step(args.engine_src, s, env)} for s in steps]
    total = {
        k: round(sum(r[k] for r in results), 2)
        for k in ("e2e_seconds", "engine_seconds", "git_seconds", "git_calls", "output_bytes")
    }
    print(
        json.dumps(
            {"flow": args.flow, "label": args.label, "steps": results, "total": total}, indent=2
        )
    )
    Path("bench-preview.json").unlink(missing_ok=True)
    Path("bench-preview.md").unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
