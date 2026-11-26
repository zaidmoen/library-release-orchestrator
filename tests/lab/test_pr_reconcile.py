"""PR lifecycle: discovery, reconciliation, duplicate prevention and network retries (ADR-0010).

The bare remotes play GitHub's role: a test "server" writes ``refs/pull/<n>/head`` exactly
as GitHub advertises PRs to Git clients.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from release_orchestrator.errors import GitError
from release_orchestrator.gitutil.repo import Git, is_transient, retry
from release_orchestrator.integrations import pr_reconcile as rec
from release_orchestrator.lab import LIBRARY_DIR, PAYMENT_DIR, build_lab
from tests.lab.test_lab_scenarios import Lab, _bare_remote

pytestmark = pytest.mark.integration


@pytest.fixture
def lab(tmp_path: Path) -> Lab:
    root = tmp_path / "lab"
    return Lab(root, build_lab(root, provider="external"))


def _released(lab: Lab, tmp_path: Path):  # type: ignore[no-untyped-def]
    """FIN-204 released, consumer prepared, everything pushed to bare remotes."""
    lib_bare = _bare_remote(tmp_path, lab, LIBRARY_DIR)
    pay_bare = _bare_remote(tmp_path, lab, PAYMENT_DIR)
    plan = lab.plan("FIN-204")
    lab.orch.prepare(plan, approve=True, dry_run=False)
    lab.orch.validate(plan)
    lab.orch.open_pr(plan, approve=True)
    lab.orch.publish(plan, approve=True)
    lab.orch.update_consumers(plan, approve=True, dry_run=False)
    lab.orch.push(plan, approve=True)
    return plan, lib_bare, pay_bare


def _open_pr_on_server(bare: Path, number: int, branch: str) -> None:
    sha = _git(bare, "rev-parse", f"refs/heads/{branch}")
    _git(bare, "update-ref", f"refs/pull/{number}/head", sha)


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _by_repo(summary):  # type: ignore[no-untyped-def]
    return {r["repository"]: r for r in summary["pull_requests"]}


def test_not_created_then_discovered_then_verified(lab, tmp_path):
    plan, lib_bare, pay_bare = _released(lab, tmp_path)
    first = _by_repo(lab.orch.reconcile_prs(plan))
    assert first["zaidlibrary"]["status"] == rec.NOT_CREATED
    assert first["payment-service"]["status"] == rec.NOT_CREATED

    _open_pr_on_server(lib_bare, 3, plan.branch)
    _open_pr_on_server(pay_bare, 1, "deps/zaidlibrary-1.1.0")
    second = _by_repo(lab.orch.reconcile_prs(plan))
    assert second["zaidlibrary"]["status"] == rec.DISCOVERED
    assert second["zaidlibrary"]["number"] == 3
    assert second["payment-service"]["number"] == 1

    third = lab.orch.reconcile_prs(plan)
    assert third["ok"] is True
    assert {r["status"] for r in third["pull_requests"]} == {rec.VERIFIED}
    # Duplicate prevention: the pr step now returns the discovered PR instead of a new request.
    assert lab.orch.open_pr(plan, approve=True)["identifier"] == "3"


def test_ambiguous_match_is_never_recorded(lab, tmp_path):
    plan, lib_bare, _ = _released(lab, tmp_path)
    _open_pr_on_server(lib_bare, 3, plan.branch)
    _open_pr_on_server(lib_bare, 4, plan.branch)  # someone opened a duplicate
    summary = lab.orch.reconcile_prs(plan)
    lib = _by_repo(summary)["zaidlibrary"]
    assert summary["ok"] is False and lib["status"] == rec.AMBIGUOUS
    assert "[3, 4]" in lib["detail"]
    assert lab.orch.s.prs.get("zaidlibrary", plan.branch)["number"] is None


def test_recorded_pr_drift_is_detected(lab, tmp_path):
    plan, lib_bare, _ = _released(lab, tmp_path)
    _open_pr_on_server(lib_bare, 3, plan.branch)
    lab.orch.reconcile_prs(plan)
    # The PR head on the server moves away from the release branch.
    _git(lib_bare, "update-ref", "refs/pull/3/head", plan.base_revision)
    lib = _by_repo(lab.orch.reconcile_prs(plan))["zaidlibrary"]
    assert lib["status"] == rec.HEAD_MOVED and lib["ok"] is False


def test_recorded_pr_missing_on_remote_is_reported(lab, tmp_path):
    plan, _, _ = _released(lab, tmp_path)
    lab.orch.record_pr(
        plan,
        repository="zaidlibrary",
        head=plan.branch,
        number=9,
        url="https://github.com/zaidmoen/harri-release-lab-library/pull/9",
        state="open-draft",
    )
    lib = _by_repo(lab.orch.reconcile_prs(plan))["zaidlibrary"]
    assert lib["status"] == rec.MISSING


def test_unpushed_branch_is_reported(lab, tmp_path):
    _bare_remote(tmp_path, lab, LIBRARY_DIR)
    plan = lab.plan("FIN-204")
    lab.orch.prepare(plan, approve=True, dry_run=False)
    lab.orch.validate(plan)
    lab.orch.open_pr(plan, approve=True)
    lib = _by_repo(lab.orch.reconcile_prs(plan))["zaidlibrary"]
    assert lib["status"] == rec.BRANCH_NOT_PUSHED


def test_reconcile_is_read_only_towards_the_remote(lab, tmp_path):
    plan, lib_bare, pay_bare = _released(lab, tmp_path)
    _open_pr_on_server(lib_bare, 3, plan.branch)
    before = (_git(lib_bare, "show-ref"), _git(pay_bare, "show-ref"))
    lab.orch.reconcile_prs(plan)
    lab.orch.reconcile_prs(plan)
    assert (_git(lib_bare, "show-ref"), _git(pay_bare, "show-ref")) == before


# --------------------------------------------------------------------------- retries
def test_retry_recovers_from_transient_errors_only():
    calls, sleeps = [], []

    def flaky() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise GitError("fatal: unable to access: Could not resolve host: github.com")
        return "ok"

    assert retry(flaky, attempts=3, sleep=sleeps.append) == "ok"
    assert sleeps == [1.0, 2.0]

    def rejected() -> str:
        raise GitError("! [rejected] main -> main (non-fast-forward)")

    with pytest.raises(GitError):
        retry(rejected, attempts=3, sleep=sleeps.append)
    assert sleeps == [1.0, 2.0]  # a permanent error is not retried
    assert not is_transient(GitError("remote: Repository not found."))


def test_retry_gives_up_after_bounded_attempts():
    sleeps: list[float] = []

    def down() -> None:
        raise GitError("fatal: Connection timed out")

    with pytest.raises(GitError):
        retry(down, attempts=3, sleep=sleeps.append)
    assert len(sleeps) == 2


def test_push_whose_response_was_lost_is_not_pushed_twice(tmp_path, monkeypatch):
    repo, bare = tmp_path / "repo", tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    _git(
        repo,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.invalid",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "init",
    )
    _git(repo, "remote", "add", "origin", bare.as_uri())
    git = Git(repo, sleep=lambda _s: None)
    real_run = Git.run
    pushes: list[tuple[str, ...]] = []

    def lossy_run(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        result = real_run(self, *args, **kwargs)
        if args[:1] == ("push",):
            pushes.append(args)
            if len(pushes) == 1:
                raise GitError("fatal: the remote end hung up unexpectedly")  # landed, reply lost
        return result

    monkeypatch.setattr(Git, "run", lossy_run)
    from release_orchestrator.config import normalize_remote

    result = git.push_ref("origin", "refs/heads/main", allowlist=(normalize_remote(bare.as_uri()),))
    assert result["status"] == "pushed"
    assert len(pushes) == 1  # the retry saw the ref already on the remote
    assert _git(bare, "rev-parse", "refs/heads/main") == _git(repo, "rev-parse", "HEAD")
