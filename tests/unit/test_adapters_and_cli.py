"""PR adapters (local + GitHub with a fake transport), manifest schema, validation runner, CLI."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from release_orchestrator.cli import main
from release_orchestrator.config import CommandSpec
from release_orchestrator.errors import BlockedError, ConfigError, ExitCode, IntegrationError
from release_orchestrator.integrations.github import GitHubPullRequestProvider
from release_orchestrator.integrations.pull_requests import LocalPullRequestProvider, PullRequestSpec
from release_orchestrator.planning.manifest import validate_manifest
from release_orchestrator.validation.runner import run_validation

SPEC = PullRequestSpec("zaidlibrary", "release/1.1.0", "main", "Release", "body")


# ------------------------------------------------------------------ PR adapters
def test_local_pr_provider_is_idempotent(tmp_path):
    provider = LocalPullRequestProvider(tmp_path)
    first = provider.ensure(SPEC)
    second = provider.ensure(SPEC)
    assert first.created and not second.created
    assert first.identifier == second.identifier
    assert len(provider.list()) == 1 and provider.list()[0]["draft"] is True
    assert not hasattr(provider, "merge")


class FakeGitHub:
    def __init__(self, branch_exists=True, existing=None):
        self.calls = []
        self.branch_exists = branch_exists
        self.existing = existing or []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, body))
        assert headers["Authorization"] == "Bearer test-token"
        if "/branches/" in url:
            return (200, {}) if self.branch_exists else (404, {"message": "Not Found"})
        if method == "GET":
            return 200, self.existing
        return 201, {"number": 7, "html_url": "https://github.example/pr/7", "state": "open"}


def _gh(transport):
    return GitHubPullRequestProvider({"zaidlibrary": "acme/zaidlibrary"}, token="test-token",
                                     transport=transport)


def test_github_creates_draft_pr_only():
    fake = FakeGitHub()
    ref = _gh(fake).ensure(SPEC)
    assert ref.created and ref.identifier == "7"
    method, url, body = fake.calls[-1]
    assert method == "POST" and url.endswith("/repos/acme/zaidlibrary/pulls")
    assert body["draft"] is True
    assert not any("merge" in url for _, url, _ in fake.calls)
    assert not hasattr(GitHubPullRequestProvider, "merge")


def test_github_returns_existing_pr():
    fake = FakeGitHub(existing=[{"number": 3, "html_url": "u", "state": "open",
                                 "base": {"ref": "main"}}])
    ref = _gh(fake).ensure(SPEC)
    assert not ref.created and ref.identifier == "3"
    assert all(m == "GET" for m, _, _ in fake.calls)


def test_github_requires_pushed_branch():
    with pytest.raises(BlockedError) as exc:
        _gh(FakeGitHub(branch_exists=False)).ensure(SPEC)
    assert exc.value.reason == "BRANCH_NOT_PUSHED"
    assert "git push origin" in exc.value.details["push_command"]


def test_github_auth_failure_does_not_leak_token():
    def deny(method, url, headers, body):
        return 401, {"message": "Bad credentials"}

    with pytest.raises(IntegrationError) as exc:
        _gh(deny).ensure(SPEC)
    assert "test-token" not in str(exc.value)


def test_github_requires_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(ConfigError):
        GitHubPullRequestProvider({})


# ------------------------------------------------------------------ manifest schema
def test_manifest_schema_rejects_garbage():
    assert validate_manifest([]) == ["manifest root must be an object"]
    errors = validate_manifest({"schema_version": 2})
    assert any("schema_version" in e for e in errors)


# ------------------------------------------------------------------ validation runner
def test_validation_runner_reports_failures_and_strips_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "super-secret")
    specs = (
        CommandSpec("ok", ("{python}", "-c", "print('fine')")),
        CommandSpec("leak", ("{python}", "-c", "import os; print(os.environ.get('GITHUB_TOKEN'))")),
        CommandSpec("fail", ("{python}", "-c", "import sys; sys.exit(3)")),
    )
    report = run_validation(specs, scope="library", cwd=tmp_path, library_path=tmp_path)
    assert [r.status for r in report.results] == ["PASSED", "PASSED", "FAILED"]
    assert report.status == "FAILED" and report.results[2].exit_code == 3
    assert "super-secret" not in report.results[1].output_tail


def test_validation_not_configured_is_not_passed(tmp_path):
    report = run_validation((), scope="library", cwd=tmp_path, library_path=tmp_path)
    assert report.status == "NOT_CONFIGURED" and not report.passed


def test_validation_timeout(tmp_path):
    spec = CommandSpec("slow", (sys.executable, "-c", "import time; time.sleep(5)"), timeout_seconds=1)
    report = run_validation((spec,), scope="library", cwd=tmp_path, library_path=tmp_path)
    assert report.results[0].status == "TIMED_OUT" and not report.passed


# ------------------------------------------------------------------ CLI
def _cli(capsys, *args: str) -> tuple[int, dict]:
    code = main(["--json", *args])
    return code, json.loads(capsys.readouterr().out)


def test_cli_end_to_end_and_exit_codes(tmp_path, capsys):
    root = tmp_path / "demo"
    code, out = _cli(capsys, "demo-init", "--dest", str(root))
    assert code == 0
    cfg = ["--config", out["result"]["config"]]
    plan = tmp_path / "plan.json"

    code, out = _cli(capsys, *cfg, "plan", "--request", "Release Change B before Change A",
                     "--out", str(plan))
    assert code == ExitCode.OK and out["result"]["status"] == "PLAN_READY"
    assert out["result"]["plan"]["request"]["deferred_keys"] == ["FIN-198"]

    code, out = _cli(capsys, *cfg, "validate-manifest", "--plan", str(plan))
    assert code == 0 and out["result"]["valid"]


@pytest.mark.parametrize("command", ["prepare", "pr", "publish", "update-consumers"])
def test_write_commands_require_approval(tmp_path, capsys, command):
    root = tmp_path / "demo"
    _, out = _cli(capsys, "demo-init", "--dest", str(root))
    cfg = ["--config", out["result"]["config"]]
    plan = tmp_path / "plan.json"
    _cli(capsys, *cfg, "plan", "--ticket", "FIN-204", "--out", str(plan))
    code, out = _cli(capsys, *cfg, command, "--plan", str(plan))
    assert out["ok"] is False
    if command == "prepare":
        assert code == ExitCode.APPROVAL_REQUIRED
    else:  # earlier gates (validation/publication) block before approval is even considered
        assert code in (ExitCode.APPROVAL_REQUIRED, ExitCode.BLOCKED)


def test_cli_invalid_input_exit_codes(tmp_path, capsys):
    assert main(["plan"]) == ExitCode.INVALID_INPUT
    capsys.readouterr()
    code, out = _cli(capsys, "--config", str(tmp_path / "missing.yaml"), "plan", "--ticket", "X-1")
    assert code == ExitCode.INVALID_INPUT and out["error"]["code"] == "INVALID_CONFIG"


def test_cli_ambiguous_request_exit_code(tmp_path, capsys):
    _, out = _cli(capsys, "demo-init", "--dest", str(tmp_path / "d"))
    code, out = _cli(capsys, "--config", out["result"]["config"], "analyze",
                     "--request", "release the thing Anas did")
    assert code == ExitCode.AMBIGUOUS_OR_NOT_FOUND
    assert out["error"]["code"] == "AMBIGUOUS_REQUEST"


def test_cli_blocked_plan_exit_code(tmp_path, capsys):
    _, out = _cli(capsys, "demo-init", "--dest", str(tmp_path / "d"), "--scenario", "dependent")
    code, out = _cli(capsys, "--config", out["result"]["config"], "plan", "--ticket", "FIN-204")
    assert code == ExitCode.BLOCKED and out["result"]["status"] == "BLOCKED"


def test_skill_wrapper_exists():
    skill = Path(__file__).resolve().parents[2] / ".claude/skills/library-release"
    assert (skill / "SKILL.md").is_file()
