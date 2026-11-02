import json
import subprocess
import sys
from pathlib import Path

import pytest

from release_orchestrator.demo import build_demo
from release_orchestrator.errors import InvalidInputError
from release_orchestrator.skill_args import parse_skill_arguments

SCRIPT = Path(__file__).resolve().parents[2] / ".claude/skills/library-release/scripts/lro.py"


@pytest.mark.parametrize(("raw", "mode", "max_step"), [
    ("FIN-204", "ticket", "full"),
    ("FIN-204 --dry-run", "ticket", "dry-run"),
    ("FIN-198 FIN-204 --plan-only", "ticket", "plan"),
    ('"Release Change B before Change A"', "text", "full"),
    ("--plan-only", "ask", "plan"),
    ("", "ask", "full"),
    ("a1b2c3d 0123456789abcdef0123456789abcdef01234567", "commits", "full"),
])
def test_parse_skill_arguments(raw, mode, max_step):
    intake = parse_skill_arguments(raw).to_dict()
    assert intake["mode"] == mode and intake["max_step"] == max_step


def test_text_request_becomes_request_arg():
    intake = parse_skill_arguments('Release "Change B" before Change A --base v1.0.0')
    assert intake.cli_request_args() == ["--request", "Release Change B before Change A",
                                         "--base", "v1.0.0"]


def test_unknown_flag_rejected():
    with pytest.raises(InvalidInputError):
        parse_skill_arguments("FIN-204 --force")


def _lro(*args: str, cwd: Path) -> tuple[int, dict]:
    proc = subprocess.run([sys.executable, str(SCRIPT), *args], cwd=cwd, capture_output=True,
                          text=True, check=False)
    return proc.returncode, json.loads(proc.stdout)


def test_skill_script_drives_engine(tmp_path):
    root = tmp_path / "demo"
    build_demo(root)
    code, out = _lro("intake", "FIN-204", "--plan-only", cwd=root)
    assert code == 0 and out["intake"]["plan_args"] == ["--ticket", "FIN-204"]
    code, out = _lro("plan", "--ticket", "FIN-204", "--out", "release-plan.json", cwd=root)
    assert code == 0 and out["result"]["status"] == "PLAN_READY"
    code, out = _lro("prepare", "--plan", "release-plan.json", cwd=root)
    assert code == 4 and out["error"]["code"] == "APPROVAL_REQUIRED"
