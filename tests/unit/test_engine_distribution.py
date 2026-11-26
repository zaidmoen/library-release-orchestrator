"""ADR-0018 acceptance A-E and W (install): the single-file Skill's pinned engine bootstrap.

The bootstrap block is taken *verbatim* from ``distribution/library-release/SKILL.md``;
only its source URL and pinned SHA are replaced, by a local fixture engine repository.
Mocked: the engine source (a local Git repository, no network) and a stub engine entry
point. Real: bash, git fetch by SHA, the installer, venv creation, locking and the
wrapper.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from release_orchestrator.fsutil import remove_tree

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "distribution" / "library-release" / "SKILL.md"
INSTALLER = ROOT / "scripts" / "lro_engine.py"

_spec = importlib.util.spec_from_file_location("lro_engine", INSTALLER)
assert _spec and _spec.loader
lro_engine = importlib.util.module_from_spec(_spec)
sys.modules["lro_engine"] = lro_engine
_spec.loader.exec_module(lro_engine)

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Dev",
    "GIT_AUTHOR_EMAIL": "dev@example.invalid",
    "GIT_COMMITTER_NAME": "Dev",
    "GIT_COMMITTER_EMAIL": "dev@example.invalid",
}
STUB = """import json, os, sys
print(json.dumps({"cwd": os.getcwd(), "argv": sys.argv[1:], "file": __file__}))
"""


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, env=GIT_ENV
    ).stdout.strip()


def _bash() -> str | None:
    """A POSIX bash (Git Bash on Windows, never the WSL launcher)."""
    candidates = []
    if os.name == "nt":
        git = shutil.which("git")
        if git:
            base = Path(git).resolve().parents[1]
            candidates += [base / "bin" / "bash.exe", base / "usr" / "bin" / "bash.exe"]
    found = shutil.which("bash")
    if found:
        candidates.append(Path(found))
    for c in candidates:
        if not c.exists():
            continue
        try:
            uname = subprocess.run(
                [str(c), "-c", "uname -s"], capture_output=True, text=True, timeout=30
            ).stdout
        except (OSError, subprocess.TimeoutExpired):
            continue
        if any(k in uname for k in ("MINGW", "MSYS", "CYGWIN", "Linux", "Darwin")):
            if os.name == "nt" and "Linux" in uname:
                continue  # WSL: a different filesystem view
            return str(c)
    return None


BASH = _bash()
needs_bash = pytest.mark.skipif(BASH is None, reason="POSIX bash not available")


@pytest.fixture
def engine_source(tmp_path: Path) -> tuple[Path, str]:
    """A fixture engine repository: the real installer, a stub entry point, no deps."""
    src = tmp_path / "engine-src"
    src.mkdir()
    _git(src, "init", "-q", "-b", "main")
    (src / "scripts").mkdir()
    shutil.copy(INSTALLER, src / "scripts" / "lro_engine.py")
    entry = src / ".claude" / "skills" / "library-release" / "scripts"
    entry.mkdir(parents=True)
    (entry / "lro.py").write_text(STUB, encoding="utf-8")
    (src / "pyproject.toml").write_text(
        '[project]\nname = "fixture-engine"\nversion = "0"\ndependencies = []\n',
        encoding="utf-8",
    )
    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", "engine")
    _git(src, "config", "uploadpack.allowReachableSHA1InWant", "true")
    return src, _git(src, "rev-parse", "HEAD")


def _bootstrap_block(repo_url: str, sha: str) -> str:
    text = SKILL.read_text(encoding="utf-8")
    block = re.search(r"```bash lro-bootstrap\n(.*?)```", text, re.S)
    assert block, "SKILL.md must contain the lro-bootstrap block"
    script = re.sub(r"^LRO_SHA=\S+$", f"LRO_SHA={sha}", block.group(1), flags=re.M)
    return re.sub(r"^LRO_REPO=\S+$", f"LRO_REPO={repo_url}", script, flags=re.M)


def _run_bootstrap(
    home: Path, repo_url: str, sha: str, cwd: Path
) -> subprocess.CompletedProcess[str]:
    assert BASH
    env = {
        **os.environ,
        "LRO_HOME": str(home),
        "LRO_ALLOWED_ORIGINS": repo_url,
    }
    return subprocess.run(
        [BASH, "-c", _bootstrap_block(repo_url, sha)],
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
        timeout=300,
    )


def _url(path: Path) -> str:
    return path.as_posix()


# --------------------------------------------------------------------------- A, B, D, E
@needs_bash
def test_a_b_e_first_install_then_cached_and_cwd_preserved(engine_source, tmp_path):
    src, sha = engine_source
    home, work = tmp_path / "home", tmp_path / "company-repo"
    work.mkdir()
    first = _run_bootstrap(home, _url(src), sha, work)
    assert first.returncode == 0, first.stdout + first.stderr
    assert f"LRO_ENGINE=installed {sha}" in first.stdout
    data = json.loads((home / "engines" / sha / "engine.json").read_text(encoding="utf-8"))
    assert data["sha"] == sha and Path(data["python"]).is_file()
    assert not any(work.iterdir())  # nothing written into the engineer's repository
    # E: the wrapper runs the pinned engine with absolute paths, in the caller's cwd
    run = subprocess.run(
        [BASH, str(home / "engines" / sha / "bin" / "lro"), "release-mode", "discover"],
        capture_output=True,
        text=True,
        cwd=work,
        timeout=120,
    )
    out = json.loads(run.stdout)
    assert Path(out["cwd"]).resolve() == work.resolve()
    assert out["argv"] == ["release-mode", "discover"]
    assert Path(out["file"]).resolve().is_relative_to((home / "engines" / sha / "src").resolve())
    # B: cached, offline (the source is gone), only verified
    remove_tree(src)
    second = _run_bootstrap(home, _url(src), sha, work)
    assert second.returncode == 0, second.stdout + second.stderr
    assert f"LRO_ENGINE=cached {sha}" in second.stdout


# --------------------------------------------------------------------------- C
@needs_bash
def test_c_unknown_revision_is_refused_and_nothing_is_installed(engine_source, tmp_path):
    src, _sha = engine_source
    home, work = tmp_path / "home", tmp_path / "w"
    work.mkdir()
    bogus = "0" * 40
    result = _run_bootstrap(home, _url(src), bogus, work)
    assert result.returncode == 3
    assert "LRO_ERROR=FETCH_OR_VERIFY_FAILED" in result.stdout
    assert not (home / "engines" / bogus).exists()
    assert not list((home / "tmp").glob("fetch.*"))  # temporary checkout removed


@needs_bash
def test_c_tampered_cache_is_detected_before_execution(engine_source, tmp_path):
    src, sha = engine_source
    home, work = tmp_path / "home", tmp_path / "w"
    work.mkdir()
    assert _run_bootstrap(home, _url(src), sha, work).returncode == 0
    entry = home / "engines" / sha / "src" / ".claude/skills/library-release/scripts/lro.py"
    entry.write_text("print('tampered')\n", encoding="utf-8")
    again = _run_bootstrap(home, _url(src), sha, work)
    assert again.returncode == 3
    assert "LRO_ERROR=ENGINE_VERIFY_FAILED" in again.stdout


def test_c_installer_refuses_wrong_revision_dirty_tree_and_untrusted_origin(
    engine_source, tmp_path
):
    src, sha = engine_source
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(src), str(clone)], check=True, env=GIT_ENV)
    origin = _git(clone, "config", "--get", "remote.origin.url")
    with pytest.raises(lro_engine.InstallError) as exc:
        lro_engine.verify_checkout(clone, "1" * 40, origins=(origin,))
    assert exc.value.code == "REVISION_MISMATCH"
    with pytest.raises(lro_engine.InstallError) as exc:
        lro_engine.verify_checkout(clone, "main", origins=(origin,))
    assert exc.value.code == "BAD_SHA"  # a branch name is never accepted
    with pytest.raises(lro_engine.InstallError) as exc:
        lro_engine.verify_checkout(clone, sha, origins=("https://example.invalid/x.git",))
    assert exc.value.code == "UNTRUSTED_ORIGIN"
    (clone / "extra.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(lro_engine.InstallError) as exc:
        lro_engine.verify_checkout(clone, sha, origins=(origin,))
    assert exc.value.code == "CHECKOUT_MODIFIED"


# --------------------------------------------------------------------------- D
def test_d_platform_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("LRO_HOME", str(tmp_path / "custom home"))
    assert lro_engine.lro_home() == (tmp_path / "custom home").resolve()
    venv = tmp_path / "venv"

    class _Os:
        """Replaces only the installer module's ``os`` reference: the shared ``os``
        module (used by pathlib and pytest) is never modified."""

        def __init__(self, name: str) -> None:
            self.name = name

        def __getattr__(self, attr: str) -> Any:
            return getattr(os, attr)

    monkeypatch.setattr(lro_engine, "os", _Os("nt"))
    assert lro_engine.venv_python(venv) == venv / "Scripts" / "python.exe"
    monkeypatch.setattr(lro_engine, "os", _Os("posix"))
    assert lro_engine.venv_python(venv) == venv / "bin" / "python"
    # a Windows-layout venv is recognised by its files on any platform
    (venv / "Scripts").mkdir(parents=True)
    (venv / "Scripts" / "python.exe").write_bytes(b"")
    assert lro_engine.venv_python(venv) == venv / "Scripts" / "python.exe"
    monkeypatch.undo()
    assert os.name in ("nt", "posix")  # the real module is untouched
    # the real platform: the layout the current interpreter's venvs actually use
    expected = ("Scripts", "python.exe") if os.name == "nt" else ("bin", "python")
    assert lro_engine.venv_python(tmp_path / "real") == tmp_path / "real" / Path(*expected)
    wrapper = lro_engine._wrapper(Path("C:/Users/a b/.lro/py.exe"), Path("C:/x y/lro.py"))
    assert '"C:/Users/a b/.lro/py.exe" -I "C:/x y/lro.py" "$@"' in wrapper
    assert "cd " not in wrapper  # never changes the working directory


def test_python_version_gate(monkeypatch):
    monkeypatch.setattr(lro_engine.sys, "version_info", (3, 10, 9))
    with pytest.raises(lro_engine.InstallError) as exc:
        lro_engine.check_python()
    assert exc.value.code == "PYTHON_TOO_OLD"


# --------------------------------------------------------------------------- W (install)
def test_w_concurrent_installs_share_one_verified_engine(engine_source, tmp_path):
    src, sha = engine_source
    home = tmp_path / "home"
    results: list[dict[str, Any]] = []
    errors: list[BaseException] = []

    def one(n: int) -> None:
        checkout = tmp_path / f"co{n}"
        subprocess.run(["git", "clone", "-q", str(src), str(checkout)], check=True, env=GIT_ENV)
        origin = _git(checkout, "config", "--get", "remote.origin.url")
        try:
            results.append(lro_engine.install(sha, checkout, home=home, origins=(origin,)))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=one, args=(n,)) for n in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert sorted(r["status"] for r in results) == ["installed", "reused", "reused"]
    assert len({r["python"] for r in results}) == 1
    assert not (home / "locks" / f"{sha}.lock").exists()


# --------------------------------------------------------------------------- distribution
def test_distributed_skill_is_self_contained_and_pinned():
    text = SKILL.read_text(encoding="utf-8")
    pins = set(re.findall(r"LRO_SHA=(\S+)", text))
    assert len(pins) == 1
    pin = pins.pop()
    assert re.fullmatch(r"[0-9a-f]{40}|__PINNED_SHA__", pin)
    assert text.count(pin) >= 3  # bootstrap, wrapper path, version-matched references
    for forbidden in ("--force", "push -f", "tag -d", "merge_pull_request", "reset --hard"):
        assert forbidden not in text
    assert "@main" not in text and "/main/" not in text  # never a moving branch
    assert "never" in text.lower() and "approval" in text.lower()
