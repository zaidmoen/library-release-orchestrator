"""ADR-0017 acceptance scenarios A-L: config-free bootstrap, README-first consumers,
impact matrix, branch/PR strategy, release notes, safe reorder with immutable tags,
missing integrations and approval enforcement."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from release_orchestrator.cli import main
from release_orchestrator.gitutil.repo import clear_memo
from release_orchestrator.integrations.github_status import GitHubAccessError, GitHubReader
from release_orchestrator.lab import INVOICE_DIR, LIBRARY_DIR, PAYMENT_DIR, REPORTING_DIR, build_lab

pytestmark = pytest.mark.integration

ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Dev",
    "GIT_AUTHOR_EMAIL": "dev@example.invalid",
    "GIT_COMMITTER_NAME": "Dev",
    "GIT_COMMITTER_EMAIL": "dev@example.invalid",
}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, env=ENV
    ).stdout.strip()


def commit(repo: Path, files: dict[str, str], message: str) -> str:
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def tags(repo: Path) -> dict[str, str]:
    out = git(repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/tags")
    return dict(line.split() for line in out.splitlines())


def refs_and_tree(repo: Path) -> tuple[str, str, str]:
    """What a read-only command must never change (refs, HEAD, working tree)."""
    return (
        git(repo, "for-each-ref", "--format=%(refname) %(objectname)"),
        git(repo, "rev-parse", "HEAD"),
        git(repo, "status", "--porcelain"),
    )


class Cli:
    def __init__(self, cwd: Path, monkeypatch: Any, capsys: Any) -> None:
        self.cwd, self.mp, self.capsys = cwd, monkeypatch, capsys

    def __call__(self, *args: str) -> tuple[int, dict[str, Any]]:
        self.mp.chdir(self.cwd)
        self.mp.delenv("LRO_CONFIG", raising=False)
        self.capsys.readouterr()
        clear_memo()
        code = main(["--json", *args])
        out = json.loads(self.capsys.readouterr().out)
        return code, out


@pytest.fixture
def bare_lab(tmp_path: Path, monkeypatch, capsys) -> tuple[Path, Cli]:  # type: ignore[no-untyped-def]
    """A Release Lab library repository with no YAML: the configuration-free path."""
    root = tmp_path / "lab"
    build_lab(root)
    (root / "release-orchestrator.yaml").unlink()
    return root, Cli(root / LIBRARY_DIR, monkeypatch, capsys)


def trust(cli: Cli) -> None:
    code, out = cli(
        "trust-validation",
        "--scope",
        "library",
        "--name",
        "unit-tests",
        "--env",
        "PYTHONPATH={library_path}/src",
        "--approve",
        "--",
        "{python}",
        "-m",
        "unittest",
        "discover",
        "-s",
        "tests",
        "-t",
        ".",
    )
    assert code == 0, out


# --------------------------------------------------------------------------- A, B, K
def test_a_b_fresh_repository_needs_no_yaml_and_verifies_readme_consumers(bare_lab):
    root, cli = bare_lab
    lib = root / LIBRARY_DIR
    before = refs_and_tree(lib)
    code, out = cli("bootstrap")
    assert code == 0 and out["result"]["source"] == "derived"
    result = out["result"]
    library = result["config"]["library"]
    assert (library["name"], library["package_dir"], library["tag_prefix"]) == (
        "zaidlibrary",
        "src/zaidlibrary",
        "v",
    )
    assert result["provenance"]["library.name"] == "pyproject.toml [project]"
    assert result["config"]["registry"]["provider"] == "local"  # never production
    consumers = {c["name"]: c for c in result["consumer_discovery"]["consumers"]}
    assert set(consumers) == {PAYMENT_DIR, INVOICE_DIR, REPORTING_DIR}
    assert all(c["status"] == "CONFIRMED" for c in consumers.values())
    assert consumers[PAYMENT_DIR]["declaration"]["specifier"] == "==1.0.0"
    assert result["consumer_discovery"]["complete"] is True
    assert [q["id"] for q in result["questions"]] == ["validation"]  # never inferred
    assert "validation" not in result["config"]
    assert refs_and_tree(lib) == before  # read-only for the repository
    assert not (lib / "release-orchestrator.yaml").exists()


def test_a_plans_stay_blocked_until_the_engineer_trusts_validation(bare_lab):
    _, cli = bare_lab
    code, out = cli("plan", "--ticket", "FIN-204", "--prerelease", "rc")
    assert code == 3
    assert [b["reason"] for b in out["result"]["plan"]["blockers"]] == ["NO_VALIDATION_CONFIGURED"]
    code, out = cli(
        "trust-validation", "--scope", "library", "--name", "t", "--", "{python}", "-c", "pass"
    )
    assert code == 4 and out["error"]["code"] == "APPROVAL_REQUIRED"  # L: approval enforced
    code, out = cli(
        "trust-validation", "--scope", "library", "--name", "t", "--approve", "--", "rm", "{x}"
    )
    assert code == 2  # unknown placeholder refused like in YAML
    trust(cli)
    code, out = cli("plan", "--ticket", "FIN-204", "--prerelease", "rc")
    assert code == 0 and out["result"]["status"] == "PLAN_READY"


def test_explicit_yaml_always_wins(tmp_path, monkeypatch, capsys):
    root = tmp_path / "lab"
    build_lab(root)
    cli = Cli(root / LIBRARY_DIR, monkeypatch, capsys)
    code, out = cli("bootstrap")
    assert code == 0 and out["result"]["source"].startswith("YAML")


def test_k_github_unavailable_is_reported_not_fatal(bare_lab, monkeypatch):
    _, cli = bare_lab

    def denied(self, *a, **k):  # type: ignore[no-untyped-def]
        raise GitHubAccessError("GitHub GET returned 404: Not Found", details={"status": 404})

    trust(cli)  # isolate the GitHub behaviour from the validation gate
    monkeypatch.setattr(GitHubReader, "list_pulls", denied)
    monkeypatch.setattr(GitHubReader, "repository", denied)
    _code, out = cli("--repository", "acme/zaidlibrary", "assess", "--ticket", "FIN-204")
    result = out["result"]
    assert result["status"] in ("NEEDS REVIEW", "UNKNOWN")
    assert result["resolution"]["sources"]["pull_requests"].startswith("unavailable")
    assert any("PR evidence unavailable" in w for w in result["resolution"]["warnings"])
    # K (Jira): no tracker at all is stated, never assumed
    assert result["freshness"]["issue_tracker"] == "not configured"


# --------------------------------------------------------------------------- C
def test_c_missing_readme_consumers_asks_and_never_claims_completeness(bare_lab):
    root, cli = bare_lab
    lib = root / LIBRARY_DIR
    commit(lib, {"README.md": "# zaidlibrary\n\nNo consumer list here.\n"}, "docs: trim readme")
    _code, out = cli("consumers")
    result = out["result"]
    assert result["candidates"] == [] and result["complete"] is False
    assert result["question"]["question"].startswith("لم أجد قائمة واضحة")
    assert any("filename:requirements.txt" in q for q in result["search_plan"])
    _code, out = cli("consumers", "--add", PAYMENT_DIR, "--add", "ghost-service")
    by_name = {c["name"]: c for c in out["result"]["consumers"]}
    assert by_name[PAYMENT_DIR]["status"] == "CONFIRMED"
    assert by_name["ghost-service"]["status"] == "INACCESSIBLE"  # never "unaffected"
    assert out["result"]["complete"] is False


def test_c_remote_evidence_and_search_hits(bare_lab, tmp_path):
    root, cli = bare_lab
    lib = root / LIBRARY_DIR
    commit(
        lib,
        {"README.md": "# z\n\n## Used by\n\n- https://github.com/acme/ledger-service\n"},
        "docs: consumers",
    )
    payload = tmp_path / "file.json"
    payload.write_text(json.dumps("zaidlibrary>=1.0,<2\n"), encoding="utf-8")
    code, out = cli(
        "mcp-ingest",
        "--kind",
        "github-file",
        "--repository",
        "acme/ledger-service",
        "--key",
        "requirements.txt",
        "--tool",
        "get_file_contents",
        "--from",
        str(payload),
    )
    assert code == 0, out
    hits = tmp_path / "hits.json"
    hits.write_text(
        json.dumps(
            {
                "items": [
                    {"path": "requirements.txt", "repository": {"full_name": "acme/maybe-service"}}
                ]
            }
        ),
        encoding="utf-8",
    )
    cli("mcp-ingest", "--kind", "github-code-search", "--from", str(hits))
    code, out = cli("consumers")
    by_repo = {c["repository"]: c for c in out["result"]["consumers"]}
    ledger = by_repo["acme/ledger-service"]
    assert ledger["status"] == "CONFIRMED" and ledger["imports"] is None  # declared only
    assert by_repo["acme/maybe-service"]["status"] == "POSSIBLE"
    assert out["result"]["complete"] is False


# --------------------------------------------------------------------------- G, H + impact
@pytest.fixture
def yaml_lab(tmp_path: Path, monkeypatch, capsys) -> tuple[Path, Cli]:  # type: ignore[no-untyped-def]
    root = tmp_path / "lab"
    build_lab(root)
    return root, Cli(root / LIBRARY_DIR, monkeypatch, capsys)


def test_g_only_the_affected_consumer_is_proposed(bare_lab, tmp_path):
    _root, cli = bare_lab
    trust(cli)
    plan = tmp_path / "p.json"
    cli("plan", "--ticket", "FIN-204", "--prerelease", "rc", "--out", str(plan))
    _code, out = cli("impact", "--plan", str(plan))
    rows = {r["service"]: r for r in out["result"]["rows"]}
    assert rows[PAYMENT_DIR]["decision"] == "UPGRADE_RECOMMENDED"
    assert rows[PAYMENT_DIR]["affected_modules"] == ["zaidlibrary.payments"]
    assert rows[INVOICE_DIR]["decision"] == "NOT_REQUIRED_STATIC"
    assert rows[REPORTING_DIR]["decision"] == "NOT_REQUIRED_STATIC"
    assert "runtime not verified" in " ".join(rows[REPORTING_DIR]["evidence"])
    assert out["result"]["upgrade_recommended"] == [PAYMENT_DIR]


def test_h_release_notes_contain_only_the_shipped_changes(yaml_lab, tmp_path):
    root, cli = yaml_lab
    lib = root / LIBRARY_DIR
    plan = tmp_path / "p.json"
    _code, out = cli(
        "plan",
        "--ticket",
        "FIN-198",
        "--ticket",
        "FIN-204",
        "--ticket",
        "FIN-215",
        "--out",
        str(plan),
    )
    assert out["result"]["status"] == "PLAN_READY", out
    before = refs_and_tree(lib)
    _code, out = cli("release-notes", "--plan", str(plan))
    notes, md = out["result"]["notes"], out["result"]["markdown"]
    assert [e["key"] for e in notes["entries"]] == ["FIN-198", "FIN-204", "FIN-215"]
    for excluded in ("FIN-211", "FIN-219", "FIN-223", "FIN-230"):
        assert excluded not in md  # never shown as shipped
    assert "## Features" in md and "## Fixes" in md  # feat / fix commits
    assert notes["published"] is False and out["result"]["status"].startswith("draft")
    assert refs_and_tree(lib) == before


def test_h_breaking_changes_need_evidence_and_flag_consumers(yaml_lab, tmp_path):
    _, cli = yaml_lab
    plan = tmp_path / "p.json"
    cli(
        "plan",
        "--ticket",
        "FIN-198",
        "--ticket",
        "FIN-211",
        "--ticket",
        "FIN-230",
        "--out",
        str(plan),
    )
    _code, out = cli("release-notes", "--plan", str(plan))
    breaking = " ".join(out["result"]["notes"]["breaking_changes"])
    assert "FIN-230" in breaking and "invoice_due_date" in breaking
    _code, out = cli("impact", "--plan", str(plan))
    rows = {r["service"]: r for r in out["result"]["rows"]}
    assert rows["invoice-service"]["decision"] == "BREAKING_IMPACT"


def test_l_release_notes_publication_gates(yaml_lab, tmp_path):
    root, cli = yaml_lab
    lib = root / LIBRARY_DIR
    plan = tmp_path / "p.json"
    cli("plan", "--ticket", "FIN-204", "--out", str(plan))
    args = ("publish-release-notes", "--plan", str(plan), "--github-repository", "acme/zaidlibrary")
    code, out = cli(*args)
    assert out["error"]["code"] == "APPROVAL_REQUIRED"
    code, out = cli(*args, "--approve")
    assert out["error"]["reason"] == "RELEASE_NOT_PUBLISHED"
    for step in (("prepare", "--approve"), ("validate",), ("publish", "--approve")):
        code, out = cli(step[0], "--plan", str(plan), *step[1:])
        assert code == 0, out
    code, out = cli(*args, "--approve")
    assert out["error"]["reason"] == "TAG_NOT_PUSHED"  # no remote: never assumed pushed
    code, out = cli("publish", "--plan", str(plan))
    assert out["error"]["code"] == "APPROVAL_REQUIRED"  # L: publish needs a yes too
    assert "refs/tags/v1.1.0" in tags(lib)


# --------------------------------------------------------------------------- D, E
def test_d_e_branch_strategy(yaml_lab, tmp_path):
    root, cli = yaml_lab
    invoice = root / INVOICE_DIR
    plan = tmp_path / "p.json"
    cli("plan", "--ticket", "FIN-198", "--ticket", "FIN-211", "--out", str(plan))
    # E: no feature branch -> a deterministic new branch from the confirmed base
    _code, out = cli("branch-plan", "--plan", str(plan))
    repos = {r["repository"]: r for r in out["result"]["repositories"]}
    inv = repos["invoice-service"]
    assert (inv["strategy"], inv["target"]) == ("new", "main")
    assert inv["proposed_source"].startswith("deps/zaidlibrary-")
    assert repos["zaidlibrary"]["kind"] == "library"
    # D: one active Jira-linked branch -> stack (feature branch untouched), alternatives
    git(invoice, "switch", "-q", "-c", "feature/FIN-211-invoice-currency")
    commit(invoice, {"x.py": "x = 1\n"}, "FIN-211: wire currency")
    git(invoice, "switch", "-q", "main")
    git(invoice, "branch", "feature/FIN-2110-unrelated")  # token match only
    before = refs_and_tree(invoice)
    _code, out = cli("branch-plan", "--plan", str(plan))
    inv = {r["repository"]: r for r in out["result"]["repositories"]}["invoice-service"]
    assert [b["name"] for b in inv["existing_branches"]] == ["feature/FIN-211-invoice-currency"]
    assert inv["strategy"] == "stack" and inv["target"] == "feature/FIN-211-invoice-currency"
    assert {a["strategy"] for a in inv["alternatives"]} == {"stack", "separate"}
    assert refs_and_tree(invoice) == before
    # two active candidates -> ambiguous, nothing assumed
    git(invoice, "switch", "-q", "-c", "bugfix/FIN-211-rounding")
    commit(invoice, {"y.py": "y = 1\n"}, "FIN-211: rounding")
    git(invoice, "switch", "-q", "main")
    _code, out = cli("branch-plan", "--plan", str(plan))
    inv = {r["repository"]: r for r in out["result"]["repositories"]}["invoice-service"]
    assert inv["strategy"] == "ambiguous"
    # GitHub-only branches and an existing PR for the update branch (duplicate guard)
    pulls = tmp_path / "pulls.json"
    pulls.write_text(
        json.dumps(
            [
                {
                    "number": 9,
                    "title": "Bump",
                    "state": "open",
                    "draft": True,
                    "merged": False,
                    "html_url": "u",
                    "head": {"ref": inv["proposed_source"], "sha": "1" * 40},
                    "base": {"ref": "main"},
                }
            ]
        ),
        encoding="utf-8",
    )
    cli(
        "mcp-ingest",
        "--kind",
        "github-pulls",
        "--repository",
        "acme/invoice-service",
        "--from",
        str(pulls),
    )
    # without a GitHub identity for invoice-service, no remote evidence is used
    _code, out = cli("branch-plan", "--plan", str(plan))
    inv = {r["repository"]: r for r in out["result"]["repositories"]}["invoice-service"]
    assert inv["duplicate_prs"] == []
    git(invoice, "remote", "add", "origin", "https://github.com/acme/invoice-service.git")
    _code, out = cli("branch-plan", "--plan", str(plan))
    inv = {r["repository"]: r for r in out["result"]["repositories"]}["invoice-service"]
    assert [p["number"] for p in inv["duplicate_prs"]] == [9]


def test_confirmed_base_is_used_and_unknown_services_refused(yaml_lab, tmp_path):
    root, cli = yaml_lab
    payment = root / PAYMENT_DIR
    git(payment, "switch", "-q", "-c", "feature/FIN-204-checkout")
    base = commit(payment, {"z.py": "z = 1\n"}, "FIN-204: checkout")
    git(payment, "switch", "-q", "main")
    plan = tmp_path / "p.json"
    cli("plan", "--ticket", "FIN-204", "--out", str(plan))
    for step in (("prepare", "--approve"), ("validate",), ("publish", "--approve")):
        cli(step[0], "--plan", str(plan), *step[1:])
    code, out = cli(
        "update-consumers",
        "--plan",
        str(plan),
        "--dry-run",
        "--base",
        "payment-service=feature/FIN-204-checkout",
    )
    assert code == 0, out
    (update,) = out["result"]["updated"]
    assert f"based on feature/FIN-204-checkout at {base[:10]}" in update["notes"]
    code, out = cli("update-consumers", "--plan", str(plan), "--dry-run", "--base", "ghost=main")
    assert out["error"]["code"] == "INVALID_INPUT"


# --------------------------------------------------------------------------- I, J
def _cumulative_repo(root: Path, candidates: int = 2) -> Path:
    """v1.0.0, then cumulative candidates rc.1..rc.N, each adding an independent module."""
    repo = root / "lib"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    commit(
        repo,
        {
            "pyproject.toml": '[project]\nname = "toolkit"\nversion = "1.0.0"\n',
            "src/toolkit/__init__.py": '__version__ = "1.0.0"\n',
            "README.md": "# toolkit\n",
        },
        "chore: base",
    )
    git(repo, "tag", "v1.0.0")
    for n in range(1, candidates + 1):
        commit(
            repo,
            {f"src/toolkit/m{n}.py": f"def f{n}():\n    return {n}\n"},
            f"ABC-{n}: feat: module {n}",
        )
        git(repo, "tag", f"v1.1.0-rc.{n}")
    config = root / "release-orchestrator.yaml"
    config.write_text(
        "schema_version: 1\n"
        "library:\n  name: toolkit\n  repo: ./lib\n  package_dir: src/toolkit\n"
        "  version_file: src/toolkit/__init__.py\n"
        'change_key_pattern: "[A-Z]+-\\\\d+"\n'
        "registry:\n  provider: local\n  path: ./registry\n"
        "state_dir: ./.lro-state\n"
        "validation:\n  library:\n    - name: smoke\n"
        '      argv: ["{python}", "-c", "pass"]\n',
        encoding="utf-8",
    )
    return repo


def test_i_cumulative_reorder_allocates_a_new_candidate_and_never_moves_tags(
    tmp_path, monkeypatch, capsys
):
    repo = _cumulative_repo(tmp_path)
    cli = Cli(repo, monkeypatch, capsys)
    original = tags(repo)
    code, out = cli(
        "release-mode",
        "options",
        "--mode",
        "reorder-releases",
        "--order",
        "v1.1.0-rc.2,v1.1.0-rc.1",
    )
    requested = {o["id"]: o for o in out["result"]["options"]}["requested"]
    assert requested["valid"] is True  # B doesn't need A
    first = requested["produces"][0]
    assert (first["candidate"], first["new_candidate"]) == ("v1.1.0-rc.2", "v1.1.0-rc.3")
    assert first["tickets"] == ["ABC-2"]  # only B, not the cumulative content
    # Prepare and publish the new candidate: existing tags never change
    plan = tmp_path / "p.json"
    code, out = cli("plan", "--ticket", "ABC-2", "--prerelease", "rc", "--out", str(plan))
    assert out["result"]["plan"]["release"]["proposed_version"] == "1.1.0-rc.3"
    for step in (("prepare", "--approve"), ("validate",), ("publish", "--approve")):
        code, out = cli(step[0], "--plan", str(plan), *step[1:])
        assert code == 0, out
    after = tags(repo)
    assert {k: after[k] for k in original} == original  # immutable
    assert set(after) - set(original) == {"refs/tags/v1.1.0-rc.3"}
    rc3 = git(repo, "show", "--name-only", "--format=", "refs/tags/v1.1.0-rc.3")
    assert "m1.py" not in rc3  # A is not in the new candidate
    # a duplicate request is recognised, nothing changes
    code, out = cli("publish", "--plan", str(plan), "--approve")
    assert out["result"]["status"] == "already_published" and tags(repo) == after


def test_i_three_candidates_and_version_collisions(tmp_path, monkeypatch, capsys):
    repo = _cumulative_repo(tmp_path, candidates=3)
    cli = Cli(repo, monkeypatch, capsys)
    git(repo, "tag", "v1.1.0-rc.4", "v1.0.0")  # an unrelated tag already uses rc.4
    original = tags(repo)
    _code, out = cli(
        "release-mode",
        "options",
        "--mode",
        "reorder-releases",
        "--order",
        "v1.1.0-rc.3,v1.1.0-rc.1,v1.1.0-rc.2",
    )
    requested = {o["id"]: o for o in out["result"]["options"]}["requested"]
    assert requested["valid"] is True
    assert [p["tickets"] for p in requested["produces"]] == [["ABC-3"], ["ABC-1"], ["ABC-2"]]
    assert requested["produces"][0]["new_candidate"] == "v1.1.0-rc.5"  # never rc.4 again
    assert tags(repo) == original


def test_j_unsafe_reorder_is_blocked_with_the_dependency(yaml_lab):
    _, cli = yaml_lab
    _code, out = cli(
        "release-mode",
        "options",
        "--mode",
        "reorder-releases",
        "--order",
        "v1.1.0-rc.23,v1.1.0-rc.22",
    )
    requested = {o["id"]: o for o in out["result"]["options"]}["requested"]
    assert requested["valid"] is False
    assert requested["produces"][0]["new_candidate"] is None  # nothing allocated
    assert any("FIN-198" in e for e in requested["evidence"])


def test_live_jira_evidence_changes_the_decision(yaml_lab, tmp_path):
    """A 'blocked by' link that only the live Jira (MCP) knows must block the release."""
    _, cli = yaml_lab
    code, out = cli("--repository", "acme/zaidlibrary", "assess", "--ticket", "FIN-204")
    assert out["result"]["status"] == "NEEDS REVIEW"
    issue = tmp_path / "fin204.json"
    issue.write_text(
        json.dumps(
            {
                "key": "FIN-204",
                "fields": {
                    "summary": "Tax-inclusive order totals",
                    "status": {"name": "In Progress"},
                    "issuelinks": [{"type": {"name": "Blocks"}, "inwardIssue": {"key": "FIN-211"}}],
                },
            }
        ),
        encoding="utf-8",
    )
    code, out = cli(
        "mcp-ingest", "--kind", "jira-issue", "--tool", "getJiraIssue", "--from", str(issue)
    )
    assert code == 0 and out["result"]["summary"]["blocked_by"] == ["FIN-211"]
    code, out = cli("--repository", "acme/zaidlibrary", "assess", "--ticket", "FIN-204")
    assert out["result"]["status"] == "BLOCKED"
    assert "Jira" in " ".join(out["result"]["why"])
