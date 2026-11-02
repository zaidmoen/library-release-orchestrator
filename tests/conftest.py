from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from release_orchestrator.config import Config, load_config
from release_orchestrator.demo import build_demo
from release_orchestrator.workflow import Orchestrator, RequestInput, Services


class Demo:
    def __init__(self, root: Path, config_path: Path) -> None:
        self.root = root
        self.config_path = config_path
        self.config: Config = load_config(config_path)
        self.orch = Orchestrator(Services.from_config(self.config))

    @property
    def lib(self) -> Path:
        return self.root / "ZaidLibrary"

    def git(self, repo: str | Path, *args: str) -> str:
        path = repo if isinstance(repo, Path) else self.root / repo
        return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True,
                              check=True).stdout.strip()

    def plan(self, *tickets: str, **kw: str):  # type: ignore[no-untyped-def]
        return self.orch.plan(RequestInput(tickets=tickets, **kw))


@pytest.fixture
def demo_factory(tmp_path: Path) -> Callable[[str], Demo]:
    def make(scenario: str = "independent") -> Demo:
        root = tmp_path / scenario
        return Demo(root, build_demo(root, scenario))

    return make


def snapshot_repo(path: Path) -> dict[str, str]:
    """Everything a mutation could change: refs, worktrees, status and every .git file."""
    def run(*args: str) -> str:
        return subprocess.run(["git", "--no-optional-locks", "-C", str(path), *args],
                              capture_output=True, text=True, check=True).stdout

    files = {}
    for item in sorted((path / ".git").rglob("*")):
        if item.is_file():
            files[str(item.relative_to(path))] = hashlib.sha256(item.read_bytes()).hexdigest()
    for item in sorted(path.rglob("*")):
        if item.is_file() and ".git" not in item.parts:
            files[str(item.relative_to(path))] = hashlib.sha256(item.read_bytes()).hexdigest()
    return {
        "refs": run("for-each-ref"),
        "worktrees": run("worktree", "list", "--porcelain"),
        "status": run("status", "--porcelain", "--untracked-files=all"),
        "files": repr(files),
    }
