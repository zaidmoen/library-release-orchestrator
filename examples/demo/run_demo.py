"""Run demo scenarios A-F through the real CLI and record what actually happened.

    python examples/demo/run_demo.py [--out examples/demo/RESULTS.md]

Each scenario is built from scratch in a temporary directory, so runs are independent and
reproducible. The results file records commands, exit codes and observed Git state; it is
generated output, never hand-written.
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path


class Runner:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.lines: list[str] = []

    def cli(self, *args: str) -> tuple[int, dict]:
        proc = subprocess.run(
            [sys.executable, "-m", "release_orchestrator", "--json", *args],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
        )
        envelope = json.loads(proc.stdout)
        summary = (
            envelope["error"]["code"]
            + (f" [{envelope['error'].get('reason')}]" if envelope["error"].get("reason") else "")
            if envelope["error"]
            else "ok"
        )
        self.lines.append(f"| `{' '.join(args)}` | {proc.returncode} | {summary} |")
        return proc.returncode, envelope

    def git(self, repo: str, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.root / repo), *args], capture_output=True, text=True, check=True
        ).stdout.strip()


def scenario(name: str, title: str, builder: str, steps) -> tuple[str, bool]:
    with tempfile.TemporaryDirectory(prefix="lro-demo-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp) / "demo"
        subprocess.run(
            [
                sys.executable,
                "-m",
                "release_orchestrator",
                "demo-init",
                "--dest",
                str(root),
                "--scenario",
                builder,
            ],
            check=True,
            capture_output=True,
        )
        runner = Runner(root)
        checks: list[tuple[str, bool]] = steps(runner)
        ok = all(passed for _, passed in checks)
        out = [
            f"## {name}: {title}",
            "",
            "| Command | Exit | Outcome |",
            "|---|---|---|",
            *runner.lines,
            "",
            "Observed:",
            "",
        ]
        out += [f"- {'PASS' if passed else 'FAIL'}: {label}" for label, passed in checks]
        return "\n".join(out) + "\n", ok


def a_independent(r: Runner) -> list[tuple[str, bool]]:
    _, plan = r.cli("plan", "--ticket", "FIN-204", "--out", "plan.json")
    p = plan["result"]["plan"]
    r.cli("prepare", "--plan", "plan.json")
    r.cli("prepare", "--plan", "plan.json", "--dry-run")
    r.cli("prepare", "--plan", "plan.json", "--approve")
    r.cli("verify", "--plan", "plan.json")
    _, val = r.cli("validate", "--plan", "plan.json")
    files = r.git("ZaidLibrary", "ls-tree", "-r", "--name-only", "release/1.1.0").split()
    return [
        ("verdict INDEPENDENT", p["analysis"]["verdict"] == "INDEPENDENT"),
        ("version 1.0.0 -> 1.1.0", p["release"]["proposed_version"] == "1.1.0"),
        ("release/1.1.0 contains FIN-204 (tests/test_tax.py)", "tests/test_tax.py" in files),
        (
            "release/1.1.0 does NOT contain FIN-198 (currency.py)",
            "src/zaidlibrary/currency.py" not in files,
        ),
        ("library tests PASSED on the release branch", val["result"].get("status") == "PASSED"),
        ("user checkout still on main", r.git("ZaidLibrary", "branch", "--show-current") == "main"),
    ]


def b_dependent(r: Runner) -> list[tuple[str, bool]]:
    code, plan = r.cli("plan", "--ticket", "FIN-204", "--out", "plan.json")
    p = plan["result"]["plan"]
    r.cli("prepare", "--plan", "plan.json", "--approve")
    code2, _ = r.cli("plan", "--ticket", "FIN-198", "--ticket", "FIN-204", "--out", "plan2.json")
    return [
        ("plan BLOCKED (exit 3)", code == 3 and p["status"] == "BLOCKED"),
        ("verdict DEPENDENT", p["analysis"]["verdict"] == "DEPENDENT"),
        (
            "textual cherry-pick would have been clean",
            p["analysis"]["probe"]["applied_cleanly"] is True,
        ),
        ("prerequisite FIN-198 identified", p["prerequisites"] == ["FIN-198"]),
        ("no release branch created", "release/" not in r.git("ZaidLibrary", "branch")),
        ("remediation 'include prerequisites' plan is ready", code2 == 0),
    ]


def c_conflict(r: Runner) -> list[tuple[str, bool]]:
    refs = r.git("ZaidLibrary", "for-each-ref")
    code, plan = r.cli("plan", "--ticket", "FIN-204", "--out", "plan.json")
    p = plan["result"]["plan"]
    return [
        (
            "plan BLOCKED with verdict CONFLICT",
            code == 3 and p["analysis"]["verdict"] == "CONFLICT",
        ),
        (
            "conflicting file reported",
            p["analysis"]["probe"]["conflicted_files"] == ["src/zaidlibrary/payments.py"],
        ),
        ("suspected cause FIN-198", any(b.get("depends_on") == "FIN-198" for b in p["blockers"])),
        ("repository refs unchanged", r.git("ZaidLibrary", "for-each-ref") == refs),
    ]


def d_consumers(r: Runner) -> list[tuple[str, bool]]:
    r.cli("plan", "--ticket", "FIN-204", "--out", "plan.json")
    r.cli("prepare", "--plan", "plan.json", "--approve")
    r.cli("validate", "--plan", "plan.json")
    r.cli("update-consumers", "--plan", "plan.json", "--approve")  # refused: not published yet
    r.cli("pr", "--plan", "plan.json", "--approve")
    r.cli("publish", "--plan", "plan.json", "--approve")
    _, upd = r.cli("update-consumers", "--plan", "plan.json", "--approve")
    updated = upd["result"]["updated"]
    return [
        ("only payment-service updated", [u["consumer"] for u in updated] == ["payment-service"]),
        (
            "consumer tests PASSED against published 1.1.0",
            updated[0]["validation"]["status"] == "PASSED",
        ),
        (
            "payment-service pins 1.1.0 on its branch",
            r.git("payment-service", "show", "deps/zaidlibrary-1.1.0:requirements.txt")
            == "zaidlibrary==1.1.0",
        ),
        ("invoice-service has no new branch", r.git("invoice-service", "branch") == "* main"),
        (
            "tag v1.1.0 points at the release head",
            r.git("ZaidLibrary", "rev-parse", "v1.1.0")
            == r.git("ZaidLibrary", "rev-parse", "release/1.1.0"),
        ),
    ]


def e_duplicate(r: Runner) -> list[tuple[str, bool]]:
    _, first = r.cli("plan", "--ticket", "FIN-204", "--out", "plan.json")
    _, second = r.cli("plan", "--ticket", "FIN-204", "--out", "plan-again.json")
    r.cli("prepare", "--plan", "plan.json", "--approve")
    _, again = r.cli("prepare", "--plan", "plan-again.json", "--approve")
    r.cli("validate", "--plan", "plan.json")
    r.cli("publish", "--plan", "plan.json", "--approve")
    _, pub = r.cli("publish", "--plan", "plan.json", "--approve")
    same = (r.root / "plan.json").read_bytes() == (r.root / "plan-again.json").read_bytes()
    return [
        ("same plan_id twice", first["result"]["plan_id"] == second["result"]["plan_id"]),
        ("plan files byte-identical", same),
        ("second prepare: already_prepared", again["result"].get("status") == "already_prepared"),
        ("second publish: already_published", pub["result"].get("status") == "already_published"),
        (
            "exactly one release branch",
            r.git("ZaidLibrary", "branch", "--list", "release/*").count("release/") == 1,
        ),
    ]


def f_failing(r: Runner) -> list[tuple[str, bool]]:
    _, plan = r.cli("plan", "--ticket", "FIN-204", "--out", "plan.json")
    p = plan["result"]["plan"]
    r.cli("prepare", "--plan", "plan.json", "--approve")
    code, val = r.cli("validate", "--plan", "plan.json")
    code_pr, _ = r.cli("pr", "--plan", "plan.json", "--approve")
    _, status = r.cli("status", "--plan", "plan.json")
    return [
        (
            "static analysis only WARNS (shared symbol modified)",
            p["analysis"]["verdict"] == "INDEPENDENT_WITH_RISKS",
        ),
        (
            "validation FAILED (exit 3, VALIDATION_FAILED)",
            code == 3 and val["error"]["reason"] == "VALIDATION_FAILED",
        ),
        ("PR refused (exit 3)", code_pr == 3),
        ("run state BLOCKED", status["result"]["state"] == "BLOCKED"),
    ]


SCENARIOS = [
    ("Scenario A", "independent changes, B released before A", "independent", a_independent),
    ("Scenario B", "B depends on A", "dependent", b_dependent),
    ("Scenario C", "conflicting changes", "conflict", c_conflict),
    ("Scenario D", "consumer updates", "independent", d_consumers),
    ("Scenario E", "duplicate execution", "independent", e_duplicate),
    ("Scenario F", "failed validation", "failing", f_failing),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path(__file__).with_name("RESULTS.md"))
    args = parser.parse_args()
    sections, all_ok = [], True
    for name, title, builder, steps in SCENARIOS:
        text, ok = scenario(name, title, builder, steps)
        print(f"{name}: {'PASS' if ok else 'FAIL'}")
        sections.append(text)
        all_ok &= ok
    header = (
        "# Demo results (generated)\n\n"
        f"Generated by `examples/demo/run_demo.py` on "
        f"{datetime.now(UTC).isoformat(timespec='seconds')} "
        f"with Python {platform.python_version()} on {platform.system()}.\n"
        f"Overall: **{'ALL PASS' if all_ok else 'FAILURES'}**\n\n"
    )
    args.out.write_text(header + "\n".join(sections), encoding="utf-8")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
