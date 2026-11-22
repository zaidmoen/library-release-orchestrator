"""Regression tests for the Phase 1 code-quality review (ADR-0016).

* cat-file reader: paths with spaces and Unicode, dead or failing readers (a read failure
  is never a "missing file"), deterministic lifecycle, bounded pool, Windows deletion;
* patch-id equivalence for merge commits; refs are never memoised;
* cache invalidation when refs move or the analysis code changes.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from release_orchestrator import cache as disk_cache
from release_orchestrator.cache import AnalysisCache
from release_orchestrator.fsutil import remove_tree
from release_orchestrator.gitutil import repo as gitrepo
from release_orchestrator.gitutil.repo import (
    MAX_READERS,
    Git,
    git_session,
    open_reader_count,
    set_optimizations,
)

pytestmark = pytest.mark.integration

ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Dev",
    "GIT_AUTHOR_EMAIL": "dev@example.invalid",
    "GIT_COMMITTER_NAME": "Dev",
    "GIT_COMMITTER_EMAIL": "dev@example.invalid",
}
FILES = {
    "plain.py": "x = 1\n",
    "dir with space/a b.py": "y = 2\n",
    "ünïcødé/ملف.py": "z = 'ü'\n",
    "crlf.txt": "a\r\nb\r\n",
    "bin.dat": bytes(range(256)),
}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "core.quotepath=false", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        env=ENV,
    ).stdout.strip()


def make_repo(root: Path) -> tuple[Path, str]:
    root.mkdir(parents=True)
    git(root, "init", "-q", "-b", "main")
    for rel, content in FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_bytes(content.encode("utf-8"))
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "files")
    return root, git(root, "rev-parse", "HEAD")


@pytest.fixture(autouse=True)
def clean_state():  # type: ignore[no-untyped-def]
    gitrepo.clear_memo()
    yield
    set_optimizations(True)
    gitrepo.clear_memo()


def original(repo: Path, sha: str, path: str) -> str | None:
    return Git(repo)._show_file(sha, path)


# --------------------------------------------------------------------------- reader
def test_paths_with_spaces_unicode_crlf_and_binary_match_git_show(tmp_path):
    repo, sha = make_repo(tmp_path / "r")
    expected = {rel: original(repo, sha, rel) for rel in FILES}
    missing = ["no such.py", "dir with space/none.py", "ünïcødé/absent.py"]
    tree = original(repo, sha, "dir with space")
    assert tree is not None and "a b.py" in tree
    with git_session():
        g = Git(repo)
        for rel, want in expected.items():
            assert g.show_file(sha, rel) == want, rel
        for rel in missing:  # missing paths are None, exactly like git show
            assert g.show_file(sha, rel) is None and original(repo, sha, rel) is None
        assert g.show_file(sha, "dir with space") == tree  # a tree: same as git show
        assert open_reader_count() == 1


def test_a_dead_reader_is_never_read_as_a_missing_file(tmp_path):
    repo, sha = make_repo(tmp_path / "r")
    with git_session():
        g = Git(repo)
        assert g.show_file(sha, "plain.py") == "x = 1\n"
        reader = gitrepo._READERS[g._key]
        reader._proc.kill()  # the process dies mid-session
        reader._proc.wait()
        assert g.show_file(sha, "dir with space/a b.py") == "y = 2\n"  # fallback, not None
        assert g.show_file(sha, "ünïcødé/ملف.py") == "z = 'ü'\n"  # a fresh reader
        assert open_reader_count() == 1


def test_a_protocol_failure_falls_back_and_is_not_memoised(tmp_path, monkeypatch):
    repo, sha = make_repo(tmp_path / "r")

    def broken(self, spec):  # type: ignore[no-untyped-def]
        raise gitrepo._ReaderError("garbled header")

    with git_session():
        monkeypatch.setattr(gitrepo._BatchReader, "read", broken)
        g = Git(repo)
        assert g.show_file(sha, "plain.py") == "x = 1\n"
        assert not [k for k in gitrepo._MEMO if k[1] == "show_file"]
        assert open_reader_count() == 0  # the failed reader was closed


def test_reader_lifecycle_is_deterministic_bounded_and_windows_safe(tmp_path):
    created: list[gitrepo._BatchReader] = []
    real_init = gitrepo._BatchReader.__init__

    def tracking_init(self, path):  # type: ignore[no-untyped-def]
        real_init(self, path)
        created.append(self)

    gitrepo._BatchReader.__init__ = tracking_init  # type: ignore[method-assign]
    try:
        for round_no in range(3):  # repeated create/analyse/delete cycles
            repos = [make_repo(tmp_path / f"c{round_no}" / f"r{i}") for i in range(MAX_READERS + 4)]
            with git_session():
                for repo, sha in repos:
                    assert Git(repo).show_file(sha, "plain.py") == "x = 1\n"
                    assert open_reader_count() <= MAX_READERS
            assert open_reader_count() == 0 and not gitrepo._READERS
            for repo, _ in repos:  # no open handle keeps the repository locked (Windows
                remove_tree(repo)  # sharing violation); remove_tree clears read-only bits
                assert not repo.exists()
        assert len(created) == 3 * (MAX_READERS + 4)
        assert all(not r.alive for r in created)  # every process has exited
        # exception safety: an error inside the session still closes its readers
        repo, sha = make_repo(tmp_path / "boom")
        with pytest.raises(RuntimeError), git_session():
            Git(repo).show_file(sha, "plain.py")
            raise RuntimeError("analysis failed")
        assert open_reader_count() == 0 and all(not r.alive for r in created)
    finally:
        gitrepo._BatchReader.__init__ = real_init  # type: ignore[method-assign]


def test_no_reader_outside_a_session(tmp_path):
    repo, sha = make_repo(tmp_path / "r")
    assert Git(repo).show_file(sha, "plain.py") == "x = 1\n"
    assert not gitrepo._READERS


# --------------------------------------------------------------------------- patch-ids
def test_patch_id_of_a_merge_commit_equals_the_original(tmp_path):
    repo, _ = make_repo(tmp_path / "r")
    git(repo, "checkout", "-q", "-b", "side")
    (repo / "side.py").write_text("s = 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "side")
    git(repo, "checkout", "-q", "main")
    (repo / "main.py").write_text("m = 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "main")
    git(repo, "merge", "-q", "--no-ff", "side", "-m", "merge")
    merge = git(repo, "rev-parse", "HEAD")
    side = git(repo, "rev-parse", "side")
    set_optimizations(False)
    want = {sha: Git(repo).patch_id(sha) for sha in (merge, side)}
    set_optimizations(True)
    previous = disk_cache.configure(AnalysisCache(tmp_path / "cache"))
    try:
        for _ in range(2):  # cold, then from the disk cache
            gitrepo.clear_memo()
            assert {sha: Git(repo).patch_id(sha) for sha in (merge, side)} == want
        assert Git(repo).patch_ids([side]) == {side: want[side]}
        # refs are mutable: computed like the original and never memoised
        assert Git(repo).patch_ids(["side"]) == {"side": want[side]}
        assert not [k for k in gitrepo._MEMO if k[1] == "patch_id" and k[2] == "side"]
    finally:
        disk_cache.configure(previous)


# --------------------------------------------------------------------------- cache keys
def test_analysis_code_change_invalidates_the_cache(tmp_path, monkeypatch):
    cache = AnalysisCache(tmp_path)
    cache.put("commit", {"v": 1}, "abc")
    assert cache.get("commit", "abc") == {"v": 1}
    monkeypatch.setattr(disk_cache, "_FINGERPRINT", "different-analysis-code")
    assert cache.get("commit", "abc") is None  # new logic never reads old facts
    assert len(disk_cache.code_fingerprint()) > 0


def test_fingerprint_covers_the_analysis_sources():
    package = Path(disk_cache.__file__).resolve().parent
    for rel in disk_cache._ANALYSIS_SOURCES:
        assert (package / rel).is_file(), rel
