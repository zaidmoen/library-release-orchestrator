"""ADR-0016 Phase 1: unified assessment, honest statuses, cache correctness, benchmarks."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from release_orchestrator import cache as disk_cache
from release_orchestrator.analysis.dependency import clear_analysis_memo
from release_orchestrator.cache import AnalysisCache, MutableCacheError
from release_orchestrator.cli import main
from release_orchestrator.gitutil.repo import STATS, clear_memo
from release_orchestrator.lab import build_lab
from release_orchestrator.workflow import RequestInput
from tests.conftest import snapshot_repo
from tests.lab.test_lab_scenarios import Lab

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def shared(tmp_path_factory: pytest.TempPathFactory) -> Lab:
    root = tmp_path_factory.mktemp("assess") / "lab"
    return Lab(root, build_lab(root))


@pytest.fixture
def lab(tmp_path: Path) -> Lab:
    root = tmp_path / "lab"
    return Lab(root, build_lab(root))


def fresh_process_state() -> None:
    """What a new CLI process starts with: no memo (the disk cache is kept)."""
    clear_memo()
    clear_analysis_memo()
    STATS.reset()


def run_assess(lab: Lab, monkeypatch, capsys, *args: str) -> tuple[int, dict]:  # type: ignore[no-untyped-def]
    monkeypatch.chdir(lab.lib)
    monkeypatch.delenv("LRO_CONFIG", raising=False)
    capsys.readouterr()
    fresh_process_state()
    code = main(["--json", "--repository", "zaidmoen/zaidlibrary", "assess", *args])
    return code, json.loads(capsys.readouterr().out)["result"]


# --------------------------------------------------------------------------- statuses
def test_pr_15_is_blocked_with_static_location_and_no_auto_decision(shared, monkeypatch, capsys):
    before = snapshot_repo(shared.lib)
    code, r = run_assess(shared, monkeypatch, capsys, "--pr", "15")
    assert code == 3 and r["status"] == "BLOCKED"
    assert r["headline"] == "PR #15 · FIN-211 can't be released on its own"
    why = " ".join(r["why"])
    assert "zaidlibrary.currency.convert (invoices.py:" in why and "FIN-198" in why
    assert "missing symbol — static, confirmed" in why and "Jira" in why
    assert "Runtime: not verified yet." in r["why"]
    # content decisions are offered, never taken
    (decision,) = r["decisions_needed"]
    labels = [o["id"] for o in decision["options"]]
    assert labels == ["with-prerequisites", "hold"]
    assert "adds FIN-198" in decision["options"][0]["description"]
    assert all(o["changes_scope"] for o in decision["options"])
    assert "does not meet the request" in decision["options"][1]["description"]
    # one compact card
    assert len(r["brief"].splitlines()) <= 8
    assert r["brief"].startswith("🔴 BLOCKED")
    assert snapshot_repo(shared.lib) == before  # read-only


def test_independent_ticket_is_needs_review_until_validated(shared, monkeypatch, capsys):
    code, r = run_assess(shared, monkeypatch, capsys, "--ticket", "FIN-204")
    assert code == 0 and r["status"] == "NEEDS REVIEW"
    assert r["headline"].endswith("→ 1.1.0-rc.24")
    assert r["review"][0] == "this exact plan has no validation run yet"
    assert any("behaviour coupling" in x for x in r["review"])  # warning kept, not dropped
    assert any("origin" in x for x in r["review"])  # discovery uncertainty is honest
    assert r["next"][0].startswith("Validate it")


def test_no_argument_lists_open_prs_and_never_picks(shared, monkeypatch, capsys):
    code, r = run_assess(shared, monkeypatch, capsys)
    assert code == 5 and r["status"] == "UNKNOWN"
    assert r["headline"] == "Which change should be released?"
    assert [o.split()[0] for o in r["decisions_needed"][0]["options"]] == ["#21", "#19", "#15"]


def test_pr_ticket_conflict_is_unknown(shared, monkeypatch, capsys):
    _code, r = run_assess(shared, monkeypatch, capsys, "--pr", "15", "--ticket", "FIN-204")
    assert r["status"] == "UNKNOWN" and "PR #15 is about FIN-211" in r["why"][0]
    _code, r = run_assess(shared, monkeypatch, capsys, "--pr", "15", "--ticket", "FIN-211")
    assert r["status"] == "BLOCKED"


def test_reorder_and_release_tag_entry_points(shared, monkeypatch, capsys):
    _code, r = run_assess(shared, monkeypatch, capsys, "--order", "v1.1.0-rc.23,v1.1.0-rc.22")
    assert r["status"] == "BLOCKED" and "FIN-211 requires FIN-198" in " ".join(r["why"])
    options = r["decisions_needed"][0]["options"]
    assert [o["id"] for o in options] == ["dependency-order", "supersede"]
    # reordering adds no ticket: it changes the order, or retires candidates
    assert "changes the requested order" in options[0]["description"]
    assert "retires the older candidates" in options[1]["description"]
    assert all("adds" not in o["description"] for o in options)
    assert all(o["changes_scope"] for o in options)
    _code, r = run_assess(shared, monkeypatch, capsys, "--candidate", "v1.1.0-rc.22")
    assert r["status"] == "NEEDS REVIEW" and "1.1.0" in r["headline"]
    assert r["decisions_needed"] == []  # nothing to decide: it ships as requested


def test_already_released_change_is_never_ready(lab, monkeypatch, capsys):
    subprocess.run(["git", "-C", str(lab.lib), "tag", "v1.1.0", "v1.1.0-rc.22"], check=True)
    _code, r = run_assess(lab, monkeypatch, capsys, "--ticket", "FIN-198")
    assert r["status"] == "NEEDS REVIEW"
    assert r["headline"] == "FIN-198 is already released in v1.1.0"
    assert r["next"] == ["Nothing to release for this change."]


def test_dependency_on_an_unknown_ticket_is_unknown_not_blocked(lab, monkeypatch, capsys):
    jira = lab.root / "jira.yaml"
    old = '  FIN-204: {summary: "Tax-inclusive order totals", consumers: [payment-service]}'
    text = jira.read_text(encoding="utf-8")
    assert old in text
    jira.write_text(
        text.replace(
            old,
            '  FIN-204: {summary: "Tax", consumers: [payment-service], blocked_by: [FIN-999]}',
        ),
        encoding="utf-8",
    )
    _code, r = run_assess(lab, monkeypatch, capsys, "--ticket", "FIN-204")
    assert r["status"] == "UNKNOWN"
    assert "FIN-999 has no commits" in r["why"][0]
    assert "invoices.py" not in r["brief"]  # no invented location


def test_ready_only_with_validation_and_clean_discovery(lab, monkeypatch, capsys, tmp_path):
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(lab.lib), str(bare)], check=True)
    subprocess.run(["git", "-C", str(lab.lib), "remote", "add", "origin", str(bare)], check=True)
    code, r = run_assess(lab, monkeypatch, capsys, "--ticket", "FIN-198")
    assert r["status"] == "NEEDS REVIEW"
    assert r["review"] == ["this exact plan has no validation run yet"]
    lab.reload()
    plan, _ = lab.orch.plan(RequestInput(tickets=("FIN-198",), prerelease="rc"))
    lab.orch.prepare(plan, approve=True, dry_run=False)
    assert lab.orch.validate(plan)["status"] == "PASSED"
    code, r = run_assess(lab, monkeypatch, capsys, "--ticket", "FIN-198")
    assert code == 0 and r["status"] == "READY", r["review"]
    assert r["next"] == ["Prepare the release branch (write: needs your yes)."]


# --------------------------------------------------------------------------- cache correctness
def test_mutable_state_is_never_served_from_the_cache(lab, monkeypatch, capsys):
    _code, warm = run_assess(lab, monkeypatch, capsys, "--ticket", "FIN-204")
    _code, warm = run_assess(lab, monkeypatch, capsys, "--ticket", "FIN-204")
    assert warm["cache"]["hits"] > 0 and warm["status"] == "NEEDS REVIEW"
    # Jira changes: the warm cache must not hide it
    jira = lab.root / "jira.yaml"
    jira.write_text(
        jira.read_text(encoding="utf-8").replace(
            "consumers: [payment-service]}",
            "consumers: [payment-service], blocked_by: [FIN-211]}",
            1,
        ),
        encoding="utf-8",
    )
    _code, r = run_assess(lab, monkeypatch, capsys, "--ticket", "FIN-204")
    assert r["status"] == "BLOCKED"
    # a ref moving backwards is seen immediately too (warm cache can't resurrect commits)
    head = subprocess.run(
        ["git", "-C", str(lab.lib), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    subprocess.run(["git", "-C", str(lab.lib), "branch", "-q", "keep", head], check=True)
    subprocess.run(["git", "-C", str(lab.lib), "reset", "-q", "--hard", "v1.1.0-rc.23"], check=True)
    _code, moved = run_assess(lab, monkeypatch, capsys, "--ticket", "FIN-230")
    assert moved["status"] == "UNKNOWN" and "has nothing to release" in moved["headline"]
    subprocess.run(["git", "-C", str(lab.lib), "reset", "-q", "--hard", head], check=True)
    # a new tag (ref) is seen immediately
    subprocess.run(["git", "-C", str(lab.lib), "tag", "v1.1.0-rc.24", "main"], check=True)
    _code, r = run_assess(lab, monkeypatch, capsys, "--ticket", "FIN-204")
    assert "v1.1.0-rc.24" in [c["tag"] for c in r["context"]["release_candidates"]]


def test_cache_refuses_mutable_namespaces_and_survives_corruption(tmp_path):
    cache = AnalysisCache(tmp_path)
    for namespace in ("pr_status", "jira", "refs", "registry", "environments"):
        with pytest.raises(MutableCacheError):
            cache.put(namespace, {"x": 1}, "k")
        with pytest.raises(MutableCacheError):
            AnalysisCache.disabled().get(namespace, "k")
    cache.put("commit", {"a": 1}, "abc")
    assert cache.get("commit", "abc") == {"a": 1}
    (entry,) = (tmp_path / "v1" / "commit").rglob("*.json")
    entry.write_text("{corrupt", encoding="utf-8")
    assert cache.get("commit", "abc") is None and cache.errors == 1


def test_cache_keys_change_with_inputs_engine_and_config(lab, monkeypatch, capsys):
    run_assess(lab, monkeypatch, capsys, "--ticket", "FIN-204")
    _code, warm = run_assess(lab, monkeypatch, capsys, "--ticket", "FIN-204")
    assert warm["cache"]["misses"] == 0
    # a new commit on the source branch: only its facts are computed
    (lab.lib / "src/zaidlibrary/extra.py").write_text("def extra():\n    return 1\n")
    subprocess.run(["git", "-C", str(lab.lib), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(lab.lib),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-qm",
            "FIN-240: extra helper",
        ],
        check=True,
    )
    _code, after = run_assess(lab, monkeypatch, capsys, "--ticket", "FIN-204")
    assert 0 < after["cache"]["misses"] < warm["cache"]["hits"]
    # a change to the analysis code (its source fingerprint) invalidates everything
    monkeypatch.setattr(disk_cache, "_FINGERPRINT", "changed-analysis-code")
    _code, bumped = run_assess(lab, monkeypatch, capsys, "--ticket", "FIN-204")
    assert bumped["cache"]["hits"] == 0
    # same status in every case: the cache never changes a decision
    assert after["status"] == bumped["status"] == warm["status"]


def test_no_cache_flag_disables_the_disk_cache(lab, monkeypatch, capsys):
    monkeypatch.chdir(lab.lib)
    fresh_process_state()
    main(["--json", "--no-cache", "--repository", "o/r", "assess", "--ticket", "FIN-204"])
    r = json.loads(capsys.readouterr().out)["result"]
    assert r["cache"]["enabled"] is False
    assert not (lab.root / ".lro-state" / "cache").exists()


# --------------------------------------------------------------------------- benchmarks
@pytest.mark.benchmark
def test_benchmark_assess_pr_15(lab, monkeypatch, capsys):
    """Git subprocess counts are deterministic and gated; times are recorded (and gated at
    2x target on Linux CI, where runners are comparable)."""
    results = {}
    for label in ("cold", "warm"):
        _code, r = run_assess(lab, monkeypatch, capsys, "--pr", "15")
        results[label] = {**r["timings"], "process_git_calls": STATS.calls}
    print(json.dumps(results, indent=2))
    assert results["cold"]["process_git_calls"] <= 120  # target (baseline flow: 2663)
    assert results["warm"]["process_git_calls"] <= 25
    if sys.platform.startswith("linux"):
        assert results["cold"]["total_seconds"] <= 12.0
        assert results["warm"]["total_seconds"] <= 3.0
