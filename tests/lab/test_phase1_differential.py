"""ADR-0016 differential regression: the optimised engine (memo, batched Git, persistent
immutable cache) must produce **exactly** the results of the original code paths.

Each scenario is evaluated three times: original mode (no memo, no batching, no cache),
optimised with a cold disk cache, and optimised again with the warm cache (fresh process
memo). Every finding, patch-id, provenance record, blocker, version, plan_id, Q1 option
and graph edge is compared.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from release_orchestrator import cache as disk_cache
from release_orchestrator.analysis.dependency import DependencyAnalyzer, clear_analysis_memo
from release_orchestrator.cache import AnalysisCache
from release_orchestrator.gitutil.repo import STATS, Git, set_optimizations
from release_orchestrator.lab import build_lab
from release_orchestrator.modes import ModeEngine
from release_orchestrator.release_graph import build_release_graph
from tests.lab.test_lab_scenarios import Lab

pytestmark = pytest.mark.integration

RC22, RC23 = "v1.1.0-rc.22", "v1.1.0-rc.23"


@contextmanager
def engine_mode(optimized: bool, cache_root: Path | None) -> Iterator[None]:
    set_optimizations(optimized)
    clear_analysis_memo()
    previous = disk_cache.configure(
        AnalysisCache(cache_root) if cache_root is not None else AnalysisCache.disabled()
    )
    try:
        yield
    finally:
        set_optimizations(True)
        clear_analysis_memo()
        disk_cache.configure(previous)


def _normalize(value: Any) -> Any:
    """JSON round-trip; drop the probe's free-text git message (it embeds temp paths)."""
    data = json.loads(json.dumps(value, default=str, sort_keys=True))

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            probe = "applied_cleanly" in node
            return {k: walk(v) for k, v in node.items() if not (probe and k == "message")}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(data)


def lab_results(lab: Lab) -> dict[str, Any]:
    lab.reload()
    eng = ModeEngine(lab.orch)
    out: dict[str, Any] = {"discover": eng.discover()}
    scan = eng._scan("v1.0.0", "main")
    out["scan"] = scan.to_dict()
    out["patch_ids"] = scan.patch_ids
    for keys in (
        ["FIN-198"],
        ["FIN-204"],
        ["FIN-211"],
        ["FIN-198", "FIN-211"],
        ["FIN-215"],
        ["FIN-219"],
        ["FIN-223"],
        ["FIN-230"],
    ):
        for pre in (None, "rc"):
            plan, err = eng._plan(keys, "v1.0.0", "main", pre)
            out[f"plan:{'+'.join(keys)}:{pre}"] = plan.to_dict() if plan else err
    out["q1:rc"] = eng.options("release-candidate", tickets=["FIN-211"])
    out["q1:release"] = eng.options("release")
    out["q1:reorder"] = eng.options("reorder-releases", order=[RC23, RC22])
    out["graph"] = build_release_graph(eng, order=[RC23, RC22])
    out["graph:sel"] = build_release_graph(eng, selection=["FIN-211"])
    return _normalize(out)


def test_lab_results_are_identical_in_every_mode(tmp_path):
    root = tmp_path / "lab"
    lab = Lab(root, build_lab(root))
    cache_root = tmp_path / "cache"
    with engine_mode(False, None):
        STATS.reset()
        original = lab_results(lab)
        original_calls = STATS.calls
    with engine_mode(True, cache_root):
        STATS.reset()
        cold = lab_results(lab)
        cold_calls = STATS.calls
    with engine_mode(True, cache_root):
        STATS.reset()
        warm = lab_results(lab)
        warm_calls = STATS.calls
    assert cold == original
    assert warm == original
    # findings are really there (the comparison isn't vacuous)
    findings = original["plan:FIN-211:rc"]["analysis"]["findings"]
    assert any(f["kind"] == "MISSING_PREREQUISITE" for f in findings)
    assert cold_calls < original_calls / 2 and warm_calls < cold_calls


# --------------------------------------------------------------------------- synthetic repo
def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    base = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Dev",
        "GIT_AUTHOR_EMAIL": "dev@example.invalid",
        "GIT_COMMITTER_NAME": "Dev",
        "GIT_COMMITTER_EMAIL": "dev@example.invalid",
    }
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env={**base, **(env or {})},
    ).stdout.strip()


def _write(repo: Path, files: dict[str, str | bytes]) -> None:
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8", newline="\n")


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "--all")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def build_edge_repo(root: Path) -> Path:
    """Conflicts, merges, multi-commit tickets, binary files, renames, ambiguous and
    unmapped commits, an already-released cherry-pick and an undeclared code dependency."""
    repo = root / "lib"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _write(
        repo,
        {
            "src/pkg/__init__.py": "",
            "src/pkg/core.py": "def total(x):\n    return x\n\n\ndef label():\n    return 'a'\n",
            "src/pkg/old.py": "def legacy():\n    return 1\n",
            "README.md": "lib\n",
        },
    )
    _commit(repo, "chore: base")
    _git(repo, "tag", "v1.0.0")

    helper = _commit_files(repo, {"src/pkg/helper.py": "def h():\n    return 2\n"}, "ABC-1: helper")
    _commit_files(
        repo,
        {
            "src/pkg/core.py": "from pkg.helper import h\n\n\ndef total(x):\n    return x + h()\n"
            "\n\ndef label():\n    return 'a'\n"
        },
        "ABC-2: use helper (undeclared dependency)",
    )
    _commit_files(
        repo,
        {"tests/test_core.py": "def test_total():\n    assert True\n"},
        "test: cover total\n\nRefs: ABC-2",
    )
    _commit_files(repo, {"assets/logo.png": bytes(range(256)) * 4}, "ABC-3: binary asset")
    _git(repo, "mv", "src/pkg/old.py", "src/pkg/new.py")
    _commit(repo, "ABC-4: rename module")
    _commit_files(repo, {"setup.cfg": "[tool]\nx = 1\n"}, "chore: tooling (no ticket)")
    _commit_files(repo, {"src/pkg/flags.py": "ON = True\n"}, "ABC-5 ABC-6: shared flag")
    _commit_files(
        repo,
        {"src/pkg/core.py": (repo / "src/pkg/core.py").read_text().replace("'a'", "'b'")},
        "ABC-7: label b",
    )
    _commit_files(
        repo,
        {"src/pkg/core.py": (repo / "src/pkg/core.py").read_text().replace("'b'", "'c'")},
        "ABC-8: label c (conflicts without the earlier change)",
    )
    _git(repo, "checkout", "-q", "-b", "feature")
    _commit_files(repo, {"src/pkg/feature.py": "def f():\n    return 3\n"}, "ABC-9: feature")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "feature", "-m", "Merge feature")
    # an already-released cherry-pick of ABC-1 on a maintenance line
    _git(repo, "checkout", "-q", "-b", "maint", "v1.0.0")
    _git(repo, "cherry-pick", "-x", helper)
    _git(repo, "tag", "v1.0.1")
    _git(repo, "checkout", "-q", "main")
    return repo


def _commit_files(repo: Path, files: dict[str, str | bytes], message: str) -> str:
    _write(repo, files)
    return _commit(repo, message)


def edge_results(repo: Path) -> dict[str, Any]:
    git = Git(repo)
    out: dict[str, Any] = {}
    from release_orchestrator.gitutil.history import scan_history

    for base in ("v1.0.0", "v1.0.1"):
        scan = scan_history(git, base_ref=base, source_ref="main", key_pattern=r"[A-Z]+-\d+")
        out[f"scan:{base}"] = scan.to_dict()
        out[f"patch_ids:{base}"] = scan.patch_ids
        out[f"released:{base}"] = scan.already_released
        analyzer = DependencyAnalyzer(git, "src/pkg")
        for key in sorted(scan.changes):
            out[f"analysis:{base}:{key}"] = analyzer.analyze(scan, [key]).to_dict()
        out[f"analysis:{base}:ABC-2+ABC-8"] = (
            DependencyAnalyzer(git, "src/pkg")
            .analyze(scan, sorted({"ABC-2", "ABC-8"} & set(scan.changes)))
            .to_dict()
        )
    return _normalize(out)


def test_edge_cases_are_identical_in_every_mode(tmp_path):
    repo = build_edge_repo(tmp_path / "edge")
    cache_root = tmp_path / "cache"
    with engine_mode(False, None):
        original = edge_results(repo)
    with engine_mode(True, cache_root):
        cold = edge_results(repo)
    with engine_mode(True, cache_root):
        warm = edge_results(repo)
    assert cold == original
    assert warm == original

    a = original["analysis:v1.0.0:ABC-2"]
    kinds = {f["kind"] for f in a["findings"]}
    assert "DIRECT_CODE_DEPENDENCY" in kinds or "MISSING_PREREQUISITE" in kinds  # undeclared
    assert "ABC-1" in a["prerequisites"]
    conflict = original["analysis:v1.0.0:ABC-8"]
    assert any(f["kind"] == "GIT_CONFLICT" for f in conflict["findings"])
    assert original["scan:v1.0.0"]["merge_commits_skipped"]  # merge recorded
    assert original["scan:v1.0.0"]["ambiguous_commits"]  # ABC-5 ABC-6
    assert original["scan:v1.0.0"]["unmapped_commits"]  # tooling commit
    assert "ABC-1" in original["released:v1.0.1"]  # patch-id / -x provenance
    assert any(
        f["kind"] == "UNVERIFIED_FILE" for f in original["analysis:v1.0.0:ABC-3"]["findings"]
    )  # binary file reported, not silently ignored
    assert all(original["patch_ids:v1.0.0"].values())
