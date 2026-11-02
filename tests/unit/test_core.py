"""Unit tests for analysis helpers, request resolution, state machine, guards and config."""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

from release_orchestrator.analysis import python_symbols as pysym
from release_orchestrator.analysis.graph import ChangeGraph
from release_orchestrator.config import load_config
from release_orchestrator.errors import (
    AmbiguousRequestError,
    ConfigError,
    ForbiddenOperationError,
    InvalidInputError,
    InvalidTransitionError,
    NotFoundError,
)
from release_orchestrator.gitutil.history import extract_keys, scan_history
from release_orchestrator.gitutil.repo import Git
from release_orchestrator.integrations.jira import FixtureIssueTracker
from release_orchestrator.models import CommitInfo
from release_orchestrator.requests import resolve_text, resolve_ticket
from release_orchestrator.state import RunState, RunStatus

KEY = r"[A-Z][A-Z0-9]+-\d+"


# ------------------------------------------------------------------ python symbols
def test_module_name_mapping():
    assert pysym.module_name("src/zaidlibrary/payments.py", "src/zaidlibrary") == "zaidlibrary.payments"
    assert pysym.module_name("src/zaidlibrary/__init__.py", "src/zaidlibrary") == "zaidlibrary"
    assert pysym.module_name("tests/test_x.py", "src/zaidlibrary") == "tests.test_x"
    assert pysym.module_name("README.md", "src/zaidlibrary") is None


def test_bindings_and_references_resolve_imports():
    source = (
        "from zaidlibrary.currency import convert as cv\n"
        "import zaidlibrary.invoices as inv\n"
        "from . import util\n"
        "def f(x):\n"
        "    return cv(x) + inv.format_invoice(1, x) + util.go()\n"
    )
    tree = ast.parse(source)
    facts = pysym.module_facts(tree, "zaidlibrary.payments")
    refs = {(r.name, r.target) for r in pysym.references_on_lines(tree, facts, {5})}
    assert ("cv", ("zaidlibrary.currency", "convert")) in refs
    assert ("format_invoice", ("zaidlibrary.invoices", "format_invoice")) in refs
    assert ("go", ("zaidlibrary.util", "go")) in refs


def test_relative_import_inside_package_init():
    tree = ast.parse("from .payments import calculate_total\n")
    facts = pysym.module_facts(tree, "zaidlibrary", is_package=True)
    assert facts.bindings["calculate_total"] == ("zaidlibrary.payments", "calculate_total")


def test_public_names_ignore_private():
    facts = pysym.module_facts(ast.parse("def a(): pass\ndef _b(): pass\nX = 1\n"), "m")
    assert pysym.public_names(facts) == {"a", "X"}


# ------------------------------------------------------------------ graph
def test_graph_closure_and_cycles():
    graph = ChangeGraph(["A", "B", "C", "D"])
    graph.add_edge("B", "A")
    graph.add_edge("C", "B")
    assert graph.closure(["C"]) == {"A", "B", "C"}
    assert graph.cycles() == []
    graph.add_edge("A", "C")
    assert graph.cycles() == [["A", "B", "C"]]


# ------------------------------------------------------------------ history
def _repo(tmp_path: Path) -> Git:
    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=t",
                        "-c", "user.email=t@example.invalid", *args], check=True,
                       capture_output=True)

    git("init", "-q", "-b", "main")
    commits = [("base.py", "base", None), ("a.py", "FIN-1: feat: a", None),
               ("b.py", "fix: b part 1", "Refs: FIN-2"), ("b2.py", "FIN-2: b part 2", None),
               ("c.py", "FIN-1 FIN-2: shared", None), ("d.py", "docs: unrelated", None)]
    for i, (name, subject, body) in enumerate(commits):
        (tmp_path / name).write_text(f"X{i} = {i}\n")
        git("add", name)
        args = ["commit", "-q", "-m", subject] + (["-m", body] if body else [])
        git(*args)
        if i == 0:
            git("tag", "v1.0.0")
    return Git(tmp_path)


def test_groups_multiple_commits(tmp_path):
    scan = scan_history(_repo(tmp_path), base_ref="v1.0.0", source_ref="main", key_pattern=KEY)
    assert list(scan.changes) == ["FIN-1", "FIN-2"]
    assert len(scan.changes["FIN-2"].commits) == 2


def test_ambiguous_commit(tmp_path):
    scan = scan_history(_repo(tmp_path), base_ref="v1.0.0", source_ref="main", key_pattern=KEY)
    assert list(scan.ambiguous.values()) == [["FIN-1", "FIN-2"]]
    assert [c.subject for c in scan.unmapped] == ["docs: unrelated"]


def test_extract_keys_from_subject_and_trailer():
    commit = CommitInfo("a" * 40, (), "n", "e", "FIN-9: x", "body\n\nRefs: FIN-9, OPS-3", ())
    assert extract_keys(commit, KEY) == ["FIN-9", "OPS-3"]


# ------------------------------------------------------------------ requests
@pytest.fixture
def tracker(tmp_path):
    path = tmp_path / "jira.yaml"
    path.write_text("issues:\n  FIN-198: {summary: A}\n  FIN-204: {summary: B, consumers: [svc, 'bad name!']}\n")
    return FixtureIssueTracker(path)


def test_natural_language_alias(tracker):
    aliases = {"change a": "FIN-198", "change b": "FIN-204"}
    req = resolve_text("Release Change B before Change A", key_pattern=KEY, aliases=aliases,
                       tracker=tracker)
    assert req.change_keys == ("FIN-204",) and req.deferred_keys == ("FIN-198",)


def test_natural_language_requires_known_reference(tracker):
    with pytest.raises(AmbiguousRequestError):
        resolve_text("ship the tax thing", key_pattern=KEY, aliases={}, tracker=tracker)
    with pytest.raises(NotFoundError):
        resolve_text("release FIN-999", key_pattern=KEY, aliases={}, tracker=tracker)
    with pytest.raises(AmbiguousRequestError):
        resolve_text("release PR #12", key_pattern=KEY, aliases={}, tracker=tracker)


def test_ticket_validation(tracker):
    assert resolve_ticket("FIN-204", key_pattern=KEY, tracker=tracker).change_keys == ("FIN-204",)
    with pytest.raises(NotFoundError):
        resolve_ticket("FIN-1", key_pattern=KEY, tracker=tracker)
    with pytest.raises(InvalidInputError):
        resolve_ticket("FIN-204; rm -rf /", key_pattern=KEY, tracker=tracker)


def test_untrusted_consumer_names_filtered(tracker):
    assert tracker.get("FIN-204").consumers == ("svc",)


# ------------------------------------------------------------------ state machine
def test_state_transitions_enforced():
    run = RunState("abc", "lib", "1.0.0", RunStatus.ANALYZING)
    run.transition(RunStatus.PLAN_READY)
    with pytest.raises(InvalidTransitionError):
        run.transition(RunStatus.PUBLISHED)
    run.transition(RunStatus.PREPARING)
    run.transition(RunStatus.VALIDATING)
    run.transition(RunStatus.PR_READY)
    assert [h["to"] for h in run.history] == ["PLAN_READY", "PREPARING", "VALIDATING", "PR_READY"]


# ------------------------------------------------------------------ git guard
@pytest.mark.parametrize("args", [
    ("push", "origin", "main"), ("merge", "x"), ("reset", "--hard"), ("branch", "-D", "x"),
    ("branch", "-f", "x", "y"), ("tag", "-d", "v1"), ("checkout", "-f", "main"),
    ("push", "--force"), ("cherry-pick", "--force"), ("stash",),
])
def test_git_wrapper_rejects_forbidden_commands(tmp_path, args):
    with pytest.raises(ForbiddenOperationError):
        Git(tmp_path).run(*args)


def test_git_wrapper_allows_own_worktree_removal(tmp_path):
    proc = Git(tmp_path).run("worktree", "remove", "--force", "nowhere", check=False)
    assert proc.returncode != 0  # allowed through the guard; git itself fails harmlessly


# ------------------------------------------------------------------ config
def _write_config(tmp_path: Path, validation: str) -> Path:
    path = tmp_path / "c.yaml"
    path.write_text(
        "schema_version: 1\nlibrary: {name: lib, repo: ., package_dir: src/lib}\n"
        f"registry: {{path: ./reg}}\nvalidation:\n  library:\n{validation}"
    )
    return path


def test_config_rejects_unknown_placeholder(tmp_path):
    path = _write_config(tmp_path, '    - {name: t, argv: ["{python}", "{ticket_description}"]}\n')
    with pytest.raises(ConfigError):
        load_config(path)


def test_config_accepts_allowlisted_command(tmp_path):
    path = _write_config(tmp_path, '    - {name: t, argv: ["{python}", "-V"], env: {X: "{library_path}"}}\n')
    config = load_config(path)
    assert config.library_validation[0].argv == ("{python}", "-V")
    assert config.state_dir == (tmp_path / ".lro-state").resolve()


def test_config_rejects_bad_schema_version(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text("schema_version: 99\n")
    with pytest.raises(ConfigError):
        load_config(path)
