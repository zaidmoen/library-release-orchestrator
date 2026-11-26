"""ADR-0018 acceptance F-J, L, T, U, X: reorder hardening on real Git repositories.

Real: Git history, tags, the planner, prepare/validate/publish to a local registry.
Nothing is pushed; no remote exists.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from release_orchestrator.lab import INVOICE_DIR, PAYMENT_DIR, REPORTING_DIR
from tests.lab.test_mcp_native import (  # fixtures are re-used, not duplicated
    Cli,
    _cumulative_repo,
    bare_lab,  # noqa: F401  (pytest fixture)
    commit,
    git,
    tags,
    trust,
)

pytestmark = pytest.mark.integration

ORDER = "v1.1.0-rc.2,v1.1.0-rc.1"


def _options(cli: Cli, order: str) -> dict[str, dict]:
    _code, out = cli("release-mode", "options", "--mode", "reorder-releases", "--order", order)
    return {o["id"]: o for o in out["result"]["options"]}


def _preview(cli: Cli, path: Path, order: str = ORDER, choice: str = "requested") -> dict:
    _code, out = cli(
        "release-mode",
        "preview",
        "--mode",
        "reorder-releases",
        "--order",
        order,
        "--choice",
        choice,
        "--out",
        str(path),
    )
    return dict(out["result"])


def _release(cli: Cli, tmp: Path, ticket: str, name: str) -> dict:
    plan = tmp / f"{name}.json"
    code, out = cli("plan", "--ticket", ticket, "--out", str(plan))
    assert code == 0, out
    for step in (("prepare", "--approve"), ("validate",), ("publish", "--approve")):
        code, out = cli(step[0], "--plan", str(plan), *step[1:])
        assert code == 0, out
    return dict(out["result"])


# --------------------------------------------------------------------------- F
def test_f_every_step_of_an_independent_reorder_is_explicit(tmp_path, monkeypatch, capsys):
    repo = _cumulative_repo(tmp_path)
    cli = Cli(repo, monkeypatch, capsys)
    options = _options(cli, ORDER)
    req = options["requested"]
    assert req["valid"] is True and req["recommended"] is True
    first, second = req["produces"]
    assert first == {
        **first,
        "candidate": "v1.1.0-rc.2",
        "tickets": ["ABC-2"],
        "excluded": ["ABC-1"],  # rc.2 is cumulative: A is NOT in this release
        "version": "1.1.0",
        "expected_stable": "v1.1.0",
        "new_candidate": "v1.1.0-rc.3",
        "cut_from": "v1.0.0",
        "uses_candidate": None,
    }
    assert second == {
        **second,
        "candidate": "v1.1.0-rc.1",
        "tickets": ["ABC-1"],
        "excluded": [],
        "version": "1.2.0",
        "expected_stable": "v1.2.0",
        "new_candidate": "v1.2.0-rc.1",  # rc.1 was cut before B shipped: a new candidate
        "cut_from": "v1.1.0",
    }
    # the natural order re-uses rc.1 as is (its content is exactly step 1)
    natural = options["dependency-order"]["produces"]
    assert natural[0]["uses_candidate"] == "v1.1.0-rc.1"
    assert natural[0]["new_candidate"] is None


def test_g_blocked_step_shows_what_it_leaves_out(tmp_path, monkeypatch, capsys):
    from release_orchestrator.lab import LIBRARY_DIR, build_lab

    root = tmp_path / "lab"
    build_lab(root)
    cli = Cli(root / LIBRARY_DIR, monkeypatch, capsys)
    req = _options(cli, "v1.1.0-rc.23,v1.1.0-rc.22")["requested"]
    assert req["valid"] is False
    first = req["produces"][0]
    assert first["tickets"] == ["FIN-211"] and first["new_candidate"] is None
    assert first["excluded"] == ["FIN-198", "FIN-204"]  # FIN-198 is the missing prerequisite
    assert any("FIN-198" in e for e in req["evidence"])


# --------------------------------------------------------------------------- H, J
def test_h_j_three_candidates_get_distinct_never_used_candidates(tmp_path, monkeypatch, capsys):
    repo = _cumulative_repo(tmp_path, candidates=3)
    cli = Cli(repo, monkeypatch, capsys)
    git(repo, "tag", "v1.1.0-rc.4", "v1.0.0")  # an unrelated tag already uses rc.4
    before = tags(repo)
    req = _options(cli, "v1.1.0-rc.3,v1.1.0-rc.1,v1.1.0-rc.2")["requested"]
    assert req["valid"] is True
    news = [p["new_candidate"] for p in req["produces"]]
    assert news == ["v1.1.0-rc.5", "v1.2.0-rc.1", "v1.3.0-rc.1"]
    assert not {f"refs/tags/{n}" for n in news} & set(before)
    assert [p["excluded"] for p in req["produces"]] == [["ABC-1", "ABC-2"], [], []]
    assert [p["cut_from"] for p in req["produces"]] == ["v1.0.0", "v1.1.0", "v1.2.0"]
    assert tags(repo) == before  # planning writes nothing


# --------------------------------------------------------------------------- I, U
def test_i_u_full_reordered_lifecycle_is_verified_step_by_step(tmp_path, monkeypatch, capsys):
    repo = _cumulative_repo(tmp_path)
    cli = Cli(repo, monkeypatch, capsys)
    original = tags(repo)
    preview = tmp_path / "preview.json"
    _preview(cli, preview)
    code, out = cli("release-mode", "progress", "--preview", str(preview))
    assert code == 0 and out["result"]["status"] == "IN_PROGRESS"
    assert [s["status"] for s in out["result"]["sequence"]] == ["NEXT", "PENDING"]

    # Step 1: only B ships as v1.1.0
    result = _release(cli, tmp_path, "ABC-2", "step1")
    assert result["status"] == "published"
    v110 = git(repo, "ls-tree", "-r", "--name-only", "refs/tags/v1.1.0")
    assert "src/toolkit/m2.py" in v110 and "src/toolkit/m1.py" not in v110
    _code, out = cli("release-mode", "progress", "--preview", str(preview))
    assert [s["status"] for s in out["result"]["sequence"]] == ["DONE", "NEXT"]

    # U: an interrupted/duplicated step is idempotent: re-publishing changes nothing
    snapshot = tags(repo)
    code, out = cli("publish", "--plan", str(tmp_path / "step1.json"), "--approve")
    assert out["result"]["status"] == "already_published" and tags(repo) == snapshot

    # Step 2: re-planned against the *current* state, A ships as v1.2.0
    _release(cli, tmp_path, "ABC-1", "step2")
    code, out = cli("release-mode", "progress", "--preview", str(preview))
    assert code == 0, out
    assert out["result"]["status"] == "COMPLETE" and out["result"]["complete"] is True

    after = tags(repo)
    assert {k: after[k] for k in original} == original  # every original tag unchanged
    assert set(after) - set(original) == {"refs/tags/v1.1.0", "refs/tags/v1.2.0"}


# --------------------------------------------------------------------------- T
def test_t_drift_between_steps_stops_the_sequence(tmp_path, monkeypatch, capsys):
    repo = _cumulative_repo(tmp_path)
    cli = Cli(repo, monkeypatch, capsys)
    preview = tmp_path / "preview.json"
    data = _preview(cli, preview)
    # someone releases the cumulative candidate as v1.1.0 outside the plan
    git(repo, "tag", "v1.1.0", "v1.1.0-rc.2")
    code, out = cli("release-mode", "progress", "--preview", str(preview))
    assert code == 3 and out["result"]["status"] == "DRIFT"
    assert any("unexpected ['ABC-1']" in p for p in out["result"]["problems"])
    # the preview itself is stale for execution
    code, out = cli("release-mode", "check-preview", "--preview", str(preview))
    assert code == 3 and out["error"]["reason"] == "STALE_PREVIEW"
    assert data["executed"] is False


def test_t_a_moved_candidate_tag_is_detected(tmp_path, monkeypatch, capsys):
    repo = _cumulative_repo(tmp_path)
    cli = Cli(repo, monkeypatch, capsys)
    preview = tmp_path / "preview.json"
    _preview(cli, preview)
    git(repo, "tag", "-f", "v1.1.0-rc.1", "v1.0.0")  # simulated external tampering
    code, out = cli("release-mode", "progress", "--preview", str(preview))
    assert code == 3
    assert any("v1.1.0-rc.1 no longer points" in p for p in out["result"]["problems"])


# --------------------------------------------------------------------------- X
def test_x_divergent_candidates_are_unsupported_with_evidence(tmp_path, monkeypatch, capsys):
    repo = _cumulative_repo(tmp_path)
    cli = Cli(repo, monkeypatch, capsys)
    git(repo, "switch", "-q", "-c", "hotfix", "v1.0.0")
    commit(repo, {"src/toolkit/hot.py": "def h():\n    return 9\n"}, "ABC-9: fix: hotfix")
    git(repo, "tag", "v1.1.0-rc.3")
    git(repo, "switch", "-q", "main")
    options = _options(cli, "v1.1.0-rc.3,v1.1.0-rc.1")
    assert [k for k, o in options.items() if o["valid"]] == ["stop"]  # only stopping is safe
    assert "unsupported" in options
    unsupported = options["unsupported"]
    assert unsupported["valid"] is False
    assert any("v1.1.0-rc.3" in e and "DIVERGENT_CANDIDATE" in e for e in unsupported["evidence"])


# --------------------------------------------------------------------------- L + classification
def test_l_consumer_classification_never_calls_unread_code_unaffected(bare_lab, tmp_path):  # noqa: F811
    _root, cli = bare_lab
    trust(cli)
    plan = tmp_path / "p.json"
    cli("plan", "--ticket", "FIN-204", "--prerelease", "rc", "--out", str(plan))
    _code, out = cli("impact", "--plan", str(plan))
    rows = {r["service"]: r for r in out["result"]["rows"]}
    assert rows[PAYMENT_DIR]["classification"] == "AFFECTED"
    assert rows[PAYMENT_DIR]["evidence_level"] == "code"
    assert rows[INVOICE_DIR]["classification"] == "NOT AFFECTED"
    assert rows[REPORTING_DIR]["classification"] == "NOT AFFECTED"
    classes = out["result"]["classification"]
    assert classes["AFFECTED"] == [PAYMENT_DIR]
    # L: a consumer listed in the README but not cloned (and no MCP evidence) is UNRESOLVED
    readme = cli.cwd / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8") + "- `harri-release-lab-ghost` - not cloned\n",
        encoding="utf-8",
    )
    git(cli.cwd, "commit", "-q", "-am", "docs: list the ghost consumer")
    _code, out = cli("impact", "--plan", str(plan))
    ghost = [r for r in out["result"]["rows"] if "ghost" in r["service"]]
    assert ghost and all(r["classification"] == "UNRESOLVED" for r in ghost)
    assert all(r["evidence_level"] != "code" for r in ghost)


# --------------------------------------------------------------------------- operation flow
def test_operation_flow_records_engine_facts_and_slack_payloads(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LRO_HOME", str(tmp_path / "home"))
    repo = _cumulative_repo(tmp_path)
    cli = Cli(repo, monkeypatch, capsys)
    _code, op = cli("op", "start", "--kind", "reorder", "--order", ORDER)
    op_id = op["result"]["id"]
    _code, out = cli("op", "start", "--kind", "reorder")
    assert out["error"]["reason"] == "CONCURRENT_OPERATION"
    cli("op", "event", "--op", op_id, "--event", "REORDER_REQUESTED")
    preview = tmp_path / "preview.json"
    _preview(cli, preview)
    code, out = cli(
        "op", "event", "--op", op_id, "--event", "PLAN_READY", "--evidence", str(preview)
    )
    assert code == 0, out
    facts = out["result"]["events"][-1]["facts"]
    assert [s["new_candidate"] for s in facts["release_steps"]] == ["v1.1.0-rc.3", "v1.2.0-rc.1"]
    search = tmp_path / "search.json"
    search.write_text(
        json.dumps(
            {
                "results": "### Result 1 of 1\nName: #library-release-orchestrator\n"
                "Permalink: [link](https://example.slack.com/archives/C0TEST00001)\n"
                "Is Archived: false\n"
            }
        ),
        encoding="utf-8",
    )
    code, out = cli("notify", "policy", "--enable", "--from", str(search))
    assert code == 4  # approval required
    code, out = cli("notify", "policy", "--enable", "--from", str(search), "--approve")
    assert code == 0 and out["result"]["channel"]["id"] == "C0TEST00001"
    _code, out = cli("notify", "plan", "--op", op_id, "--event", "PLAN_READY")
    assert out["result"]["action"] == "post"
    message = out["result"]["message"]
    assert op_id in message and "v1.1.0-rc.3" in message and "unverified engineer" in message
    report = tmp_path / "audit.md"
    code, out = cli("op", "report", "--op", op_id, "--out", str(report))
    assert code == 0 and "PLAN_READY" in report.read_text(encoding="utf-8")
    assert "not a tamper-proof" in report.read_text(encoding="utf-8")
