"""ADR-0015: repository context, PR <-> Jira resolution, GitHub release discovery,
dependency intelligence, dependency sheet and dry-run / Slack reports, on the release lab.

All of it is read-only; the tests check that too.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from release_orchestrator.cli import main
from release_orchestrator.context import discover_context, parse_remote
from release_orchestrator.integrations.dependency_sheet import CsvDependencySheet
from release_orchestrator.integrations.github_status import ReleaseInfo
from release_orchestrator.integrations.linkage import ChangeResolver, SnapshotPullSource
from release_orchestrator.lab import LIBRARY_DIR, build_lab
from release_orchestrator.modes import ModeEngine
from release_orchestrator.release_graph import build_release_graph
from release_orchestrator.versioning.semver import SemVer
from tests.conftest import snapshot_repo
from tests.lab.test_lab_scenarios import Lab

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def shared(tmp_path_factory: pytest.TempPathFactory) -> Lab:
    root = tmp_path_factory.mktemp("discovery") / "lab"
    return Lab(root, build_lab(root))


@pytest.fixture
def lab(tmp_path: Path) -> Lab:
    root = tmp_path / "lab"
    return Lab(root, build_lab(root))


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def resolver(lab: Lab) -> ChangeResolver:
    lab.reload()
    engine = ModeEngine(lab.orch)
    return ChangeResolver(
        git=lab.orch.s.git,
        key_pattern=lab.config.change_key_pattern,
        pulls=SnapshotPullSource(lab.root / "pull-requests.yaml"),
        tracker=lab.orch.s.tracker,
        discovery=engine.discover(),
        repository="zaidmoen/zaidlibrary",
        default_branch="main",
    )


# --------------------------------------------------------------------------- Phase 1
def test_context_is_discovered_from_inside_the_library_repository(shared):
    ctx, config = discover_context(shared.lib)
    assert config is not None and config.library.name == "zaidlibrary"
    assert Path(ctx.root) == shared.lib.resolve()
    assert ctx.root_source.startswith("git")
    assert ctx.config_source.startswith("discovered")
    assert ctx.branch == "main" and len(ctx.head or "") == 40
    assert ctx.latest_stable["tag"] == "v1.0.0"
    assert [c["tag"] for c in ctx.release_candidates] == ["v1.1.0-rc.22", "v1.1.0-rc.23"]
    # no remote in the offline lab: explained, and only the missing input is requested
    assert [p.code for p in ctx.problems] == ["REPOSITORY_UNRESOLVED"]
    assert ctx.problems[0].needs == "--repository owner/name"


def test_context_from_a_subdirectory_and_with_an_override(shared):
    ctx, _ = discover_context(shared.lib / "src" / "zaidlibrary", repository="zaidmoen/zaidlibrary")
    assert ctx.ok, ctx.problems
    assert ctx.repository == "zaidmoen/zaidlibrary"
    assert ctx.repository_source == "explicit override (--repository)"


def test_context_reads_owner_and_default_branch_from_the_git_remote(lab, tmp_path):
    bare = tmp_path / "zaidlibrary.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(lab.lib), str(bare)], check=True)
    git(lab.lib, "remote", "add", "origin", "https://github.com/zaidmoen/zaidlibrary.git")
    ctx, _ = discover_context(lab.lib)
    assert (ctx.host, ctx.repository) == ("github.com", "zaidmoen/zaidlibrary")
    assert ctx.repository_source == "git remote 'origin'"
    # a local bare remote: default branch from ls-remote --symref, but no GitHub identity
    git(lab.lib, "remote", "set-url", "origin", str(bare))
    ctx, _ = discover_context(lab.lib)
    assert ctx.default_branch == "main" and "ls-remote" in ctx.default_branch_source
    assert [p.code for p in ctx.problems] == ["REPOSITORY_UNRESOLVED"]


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/zaidmoen/zaidlibrary.git", ("github.com", "zaidmoen/zaidlibrary")),
        ("git@github.com:zaidmoen/zaidlibrary.git", ("github.com", "zaidmoen/zaidlibrary")),
        ("https://user:tok@github.example.com/o/r", ("github.example.com", "o/r")),
        ("file:///tmp/repo.git", None),
        ("C:/repos/lib.git", None),
        ("../lib", None),
        ("https://github.com/only-owner", None),
    ],
)
def test_parse_remote(url, expected):
    assert parse_remote(url) == expected


def test_context_outside_any_repository_and_config_mismatch(tmp_path, shared):
    ctx, config = discover_context(tmp_path)
    assert config is None and [p.code for p in ctx.problems] == ["NOT_A_GIT_REPOSITORY"]
    other = tmp_path / "other-lib"
    other.mkdir()
    git(other, "init", "-q")
    ctx, config = discover_context(other, config_path=shared.config_path)
    assert "CONFIG_MISMATCH" in [p.code for p in ctx.problems]


def test_cli_finds_the_config_without_any_flag(shared, capsys, monkeypatch):
    monkeypatch.chdir(shared.lib)
    monkeypatch.delenv("LRO_CONFIG", raising=False)
    assert main(["--json", "release-mode", "discover"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["result"]["latest_stable"]["tag"] == "v1.0.0"


# --------------------------------------------------------------------------- Phase 2
def test_pr_alone_resolves_ticket_dependencies_and_candidates(shared):
    r = resolver(shared).resolve(pr="#15")
    assert (r["scenario"], r["status"], r["tickets"]) == ("A:pr", "RESOLVED", ["FIN-211"])
    change = r["changes"][0]
    assert change["in_candidates"] == ["v1.1.0-rc.23"]
    prereq = change["prerequisites"][0]
    assert (prereq["key"], prereq["state"], prereq["classification"]) == (
        "FIN-198",
        "in-candidate",
        "CONFIRMED",
    )
    assert prereq["pull_requests"] == [12]
    assert change["missing_prerequisites"] == ["FIN-198"]
    assert "fixture" in r["sources"]["pull_requests"]


def test_ticket_alone_finds_its_pr_by_convention_and_jira_link(shared):
    r = resolver(shared).resolve(ticket="FIN-211")
    assert r["scenario"] == "B:ticket" and r["status"] == "RESOLVED"
    assert [p["number"] for p in r["changes"][0]["pull_requests"]] == [15]
    branch_only = resolver(shared).resolve(ticket="FIN-215")  # key only in the branch name
    assert [p["number"] for p in branch_only["changes"][0]["pull_requests"]] == [17]


def test_pr_and_ticket_that_disagree_are_a_conflict(shared):
    r = resolver(shared).resolve(pr="15", ticket="FIN-204")
    assert r["status"] == "CONFLICT"
    assert "PR #15 is about FIN-211" in r["conflicts"][0]
    assert r["questions"][0]["id"] == "confirm-link"
    assert resolver(shared).resolve(pr="15", ticket="FIN-211")["status"] == "RESOLVED"


def test_pr_without_ticket_asks_and_never_trusts_the_body(shared):
    r = resolver(shared).resolve(pr="19")
    assert r["status"] == "NEEDS_INPUT" and r["tickets"] == []
    assert r["questions"][0]["id"] == "ticket"
    assert r["questions"][0]["options"] == ["FIN-215"]  # offered, never adopted
    assert r["pull_request"]["mentions"] == ["FIN-215"]


def test_no_reference_lists_open_prs_and_never_picks_one(shared):
    r = resolver(shared).resolve()
    assert r["scenario"] == "D:discover" and r["status"] == "NEEDS_INPUT"
    assert [c["number"] for c in r["candidates"]] == [21, 19, 15]
    assert any("no ticket key" in o for o in r["questions"][0]["options"])


def test_ticket_without_pr_asks_for_one(shared):
    r = resolver(shared).resolve(ticket="FIN-230")
    assert r["changes"][0]["pull_requests"] == []
    assert r["questions"][0]["id"] == "pull-request"


def test_pr_url_for_another_repository_is_refused(shared):
    r = resolver(shared).resolve(pr="https://github.com/someone/else/pull/15")
    assert r["status"] == "CONFLICT" and "someone/else" in r["conflicts"][0]


# --------------------------------------------------------------------------- Phase 3
def test_a_new_rc_pushed_to_github_is_discovered_without_editing_yaml(lab, tmp_path):
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(lab.lib), str(bare)], check=True)
    git(lab.lib, "remote", "add", "origin", str(bare))
    other = tmp_path / "teammate"
    subprocess.run(["git", "clone", "-q", str(bare), str(other)], check=True)
    git(other, "tag", "v1.1.0-rc.24", "main")
    git(other, "push", "-q", "origin", "v1.1.0-rc.24")  # a teammate cut a new RC
    lab.reload()
    disc = ModeEngine(lab.orch).discover()
    assert disc["github"]["tags_missing_locally"] == ["v1.1.0-rc.24"]
    assert any("v1.1.0-rc.24" in u for u in disc["uncertainties"])
    git(lab.lib, "fetch", "-q", "origin", "refs/tags/v1.1.0-rc.24:refs/tags/v1.1.0-rc.24")
    lab.reload()
    tags = [c["tag"] for c in ModeEngine(lab.orch).discover()["pending_candidates"]]
    assert "v1.1.0-rc.24" in tags


class FakeReleases:
    def __init__(self, releases: list[ReleaseInfo]) -> None:
        self._releases = releases

    def releases(self, repository: str) -> list[ReleaseInfo]:
        assert repository == "zaidmoen/zaidlibrary"
        return self._releases


def test_github_release_metadata_is_cross_checked_with_tags(shared):
    shared.reload()
    shared.orch.s.github = FakeReleases(  # type: ignore[assignment]
        [
            ReleaseInfo("v1.0.0", "1.0.0", False, False, "2026-01-01T00:00:00Z", "main", ""),
            ReleaseInfo("v1.1.0-rc.22", "rc.22", False, False, "", "main", ""),  # wrong flag
            ReleaseInfo("v1.1.0-rc.25", "rc.25", True, False, "", "main", ""),  # no tag
        ]
    )
    engine = ModeEngine(shared.orch)
    versions = {SemVer.parse(t[1:]): t for t in ("v1.0.0", "v1.1.0-rc.22", "v1.1.0-rc.23")}
    uncertainties: list[str] = []
    out = engine._github_releases(
        "https://github.com/zaidmoen/zaidlibrary.git", versions, uncertainties
    )
    assert out["read"] is True
    assert out["without_tag"] == ["v1.1.0-rc.25"]
    assert out["tags_without_release"] == ["v1.1.0-rc.23"]
    assert out["flag_mismatches"] and "v1.1.0-rc.22" in out["flag_mismatches"][0]
    assert uncertainties


# --------------------------------------------------------------------------- Phase 4
@pytest.fixture(scope="module")
def graph(shared):  # type: ignore[no-untyped-def]
    shared.reload()
    return build_release_graph(ModeEngine(shared.orch), order=["v1.1.0-rc.23", "v1.1.0-rc.22"])


def test_functional_dependency_needs_explicit_evidence(graph):
    deps = {(d["change"], d["requires"]): d for d in graph["dependencies"]}
    assert set(deps) == {("FIN-211", "FIN-198"), ("FIN-219", "FIN-215")}
    assert deps["FIN-211", "FIN-198"]["classification"] == "CONFIRMED"
    assert sorted(deps["FIN-211", "FIN-198"]["sources"]) == ["code", "jira"]
    assert deps["FIN-219", "FIN-215"]["sources"] == ["jira"]


def test_ancestry_is_reported_but_never_a_dependency(graph):
    assert graph["ancestry"] == [
        {
            "release": "v1.1.0-rc.23",
            "contains": "v1.1.0-rc.22",
            "evidence": "git: v1.1.0-rc.22 is an ancestor of v1.1.0-rc.23",
            "functional": False,
        }
    ]
    tags = {d["requires"] for d in graph["dependencies"]} | {
        d["change"] for d in graph["dependencies"]
    }
    assert not any(t.startswith("v") for t in tags)


def test_heuristic_overlaps_never_form_cycles_or_block(graph):
    assert graph["cycles"] == []
    pairs = {tuple(o["changes"]) for o in graph["overlaps"]}
    assert ("FIN-211", "FIN-230") in pairs
    assert all(o["classification"] == "INFERRED" for o in graph["overlaps"])


def test_requested_order_conflict_and_recommended_order(graph):
    requested = graph["requested_order"]
    assert requested["valid"] is False
    assert "FIN-211 requires FIN-198" in requested["conflicts"][0]
    assert any("ancestry" in n for n in requested["notes"])
    assert [o["candidate"] for o in graph["recommended_order"]] == ["v1.1.0-rc.22", "v1.1.0-rc.23"]
    assert graph["eligibility"] == "BLOCKED"
    assert "graph LR" in graph["mermaid"]


def test_selection_without_prerequisite_is_blocked(shared):
    shared.reload()
    g = build_release_graph(ModeEngine(shared.orch), selection=["FIN-211"])
    assert g["missing_prerequisites"] == ["FIN-198"]
    edge = next(d for d in g["dependencies"] if d["change"] == "FIN-211")
    assert edge["classification"] == "BLOCKED" and edge["state"] == "pending"
    assert g["eligibility"] == "BLOCKED"


# --------------------------------------------------------------------------- Phases 4/5
def test_cycles_unknown_dependencies_and_sheet_conflicts(lab):
    jira = lab.root / "jira.yaml"
    text = jira.read_text(encoding="utf-8")
    text = text.replace(
        "    consumers: []\n    pull_requests: [12]",
        "    consumers: []\n    pull_requests: [12]\n    blocked_by: [FIN-211]",
    ).replace(
        '  FIN-204: {summary: "Tax-inclusive order totals", consumers: [payment-service]}',
        '  FIN-204:\n    summary: "Tax-inclusive order totals"\n'
        "    consumers: [payment-service]\n    blocked_by: [FIN-999]",
    )
    jira.write_text(text, encoding="utf-8")
    (lab.root / "deps.csv").write_text(
        "ticket,depends_on,release,notes\n"
        "FIN-223,FIN-204,,dashboard needs totals\n"
        "FIN-215,,1.1.0-rc.22,\n"
        "not a key,FIN-1,,\n",
        encoding="utf-8",
    )
    lab.reload()
    sheet = CsvDependencySheet(
        lab.root / "deps.csv", key_pattern=lab.config.change_key_pattern
    ).snapshot()
    before = snapshot_repo(lab.lib)
    g = build_release_graph(ModeEngine(lab.orch), sheet=sheet, selection=["FIN-198", "FIN-211"])
    assert snapshot_repo(lab.lib) == before  # read-only
    assert ["FIN-198", "FIN-211"] in g["cycles"]
    deps = {(d["change"], d["requires"]): d for d in g["dependencies"]}
    assert deps["FIN-198", "FIN-211"]["classification"] == "BLOCKED"
    assert deps["FIN-204", "FIN-999"]["classification"] == "UNKNOWN"
    assert deps["FIN-204", "FIN-999"]["state"] == "missing"
    sheet_edge = deps["FIN-223", "FIN-204"]
    assert sheet_edge["classification"] == "INFERRED" and sheet_edge["sources"] == ["sheet"]
    assert any("Jira FIN-223 has no such link" in c for c in g["conflicts"])
    assert any("FIN-215 is assigned to v1.1.0-rc.22" in c for c in g["conflicts"])
    assert any("not a ticket key" in w for w in g["warnings"])
    assert g["eligibility"] == "BLOCKED"


def test_dependency_sheet_is_optional_and_configurable(lab):
    config = lab.root / "release-orchestrator.yaml"
    (lab.root / "deps.csv").write_text("Issue,Needs\nFIN-211,FIN-198\n", encoding="utf-8")
    config.write_text(
        config.read_text(encoding="utf-8")
        + "dependency_sheet:\n  provider: csv\n  path: ./deps.csv\n"
        "  columns: {ticket: Issue, depends_on: Needs}\n",
        encoding="utf-8",
    )
    lab.reload()
    assert lab.config.dependency_sheet is not None
    config.write_text(
        config.read_text(encoding="utf-8").replace("provider: csv", "provider: gsheets"),
        encoding="utf-8",
    )
    with pytest.raises(Exception, match="not supported"):
        lab.reload()


# --------------------------------------------------------------------------- Phases 6/7
def test_cli_report_is_read_only_and_slack_ready(shared, capsys, monkeypatch, tmp_path):
    monkeypatch.chdir(shared.lib)
    monkeypatch.delenv("LRO_CONFIG", raising=False)
    calls: list[str] = []

    def fake_repository(self, repository):  # type: ignore[no-untyped-def]
        calls.append(repository)  # no network in tests
        return {"default_branch": "main"}

    monkeypatch.setattr(
        "release_orchestrator.integrations.github_status.GitHubReader.repository",
        fake_repository,
    )
    before = snapshot_repo(shared.lib)
    state = (
        sorted(p.name for p in (shared.root / ".lro-state").rglob("*"))
        if (shared.root / ".lro-state").exists()
        else []
    )
    preview = tmp_path / "p.json"
    code = main(
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
            "--environments",
            "prep,stage",
            "--out",
            str(preview),
        ]
    )
    assert code == 0
    capsys.readouterr()
    out_dir = tmp_path / "report"
    code = main(
        [
            "--json",
            "--repository",
            "zaidmoen/zaidlibrary",
            "release-report",
            "--preview",
            str(preview),
            "--pr",
            "15",
            "--out",
            str(out_dir),
        ]
    )
    result = json.loads(capsys.readouterr().out)["result"]
    assert code == 0
    report = result["report"]
    assert report["eligibility"] == "NEEDS REVIEW"  # no validation evidence yet
    assert report["repository"]["name"] == "zaidmoen/zaidlibrary"
    assert report["repository"]["default_branch"] == "main" and calls == ["zaidmoen/zaidlibrary"]
    assert report["source_change"]["pull_request"]["number"] == 15
    assert report["source_change"]["pull_request"]["data"] == "fixture"
    assert report["missing_prerequisites"] == []  # FIN-198 is selected with FIN-211
    assert [e["environment"] for e in report["environments"]] == ["prep", "stage"]
    assert sorted(p.name for p in out_dir.iterdir()) == [
        "report.json",
        "report.md",
        "slack.json",
        "slack.txt",
    ]
    slack = json.loads((out_dir / "slack.json").read_text(encoding="utf-8"))
    assert slack["text"].startswith("NEEDS REVIEW")
    assert slack["blocks"][0]["type"] == "header"
    assert "PR data: fixture" in json.dumps(slack)
    assert "Final eligibility: NEEDS REVIEW" in (out_dir / "report.md").read_text(encoding="utf-8")
    # dry run: no repository or run-state change
    assert snapshot_repo(shared.lib) == before
    after = (
        sorted(p.name for p in (shared.root / ".lro-state").rglob("*"))
        if (shared.root / ".lro-state").exists()
        else []
    )
    assert after == state


def test_report_refuses_a_stale_preview(lab, capsys, monkeypatch, tmp_path):
    monkeypatch.chdir(lab.lib)
    monkeypatch.delenv("LRO_CONFIG", raising=False)
    preview = tmp_path / "p.json"
    main(
        [
            "--json",
            "release-mode",
            "preview",
            "--mode",
            "release",
            "--choice",
            "release:v1.1.0-rc.22",
            "--out",
            str(preview),
        ]
    )
    git(lab.root / LIBRARY_DIR, "tag", "v1.1.0-rc.30", "main")  # state moved on
    capsys.readouterr()
    assert main(["--json", "release-report", "--preview", str(preview)]) == 3
    assert json.loads(capsys.readouterr().out)["error"]["reason"] == "STALE_PREVIEW"
