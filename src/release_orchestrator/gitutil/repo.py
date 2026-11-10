"""Safe, deterministic Git access.

Every call uses an argv list (never a shell) and fails loudly with ``GitError``.
Destructive operations are refused by ``_guard`` so that no code path in the orchestrator
can force-push, merge, reset, or delete branches, even by mistake.
"""

from __future__ import annotations

import io
import logging
import os
import subprocess
import tarfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from release_orchestrator.errors import ForbiddenOperationError, GitError
from release_orchestrator.models import CommitInfo, FileChange

log = logging.getLogger(__name__)

BOT_NAME = "Library Release Orchestrator"
BOT_EMAIL = "release-orchestrator@localhost"

_FORBIDDEN_SUBCOMMANDS = frozenset(
    {
        "push",
        "merge",
        "rebase",
        "reset",
        "clean",
        "stash",
        "filter-branch",
        "gc",
        "prune",
        "update-ref",
        "reflog",
    }
)
_FORBIDDEN_FLAGS = {
    "branch": {"-d", "-D", "--delete", "-f", "--force", "-m", "-M", "--move", "-c", "-C"},
    "tag": {"-d", "--delete", "-f", "--force"},
    "checkout": {"-f", "--force", "-B"},
    "switch": {"-f", "--force", "--discard-changes", "-C"},
}


_PUSH_FORBIDDEN_FLAGS = frozenset(
    {
        "-f",
        "--force",
        "--force-with-lease",
        "--force-if-includes",
        "--mirror",
        "--delete",
        "-d",
        "--all",
        "--tags",
        "--prune",
        "--follow-tags",
    }
)


def _guard(args: tuple[str, ...], *, allow_push: bool = False) -> None:
    if not args:
        raise ForbiddenOperationError("Empty git command")
    sub = args[0]
    if sub == "push" and allow_push:
        # Only Git.push_ref may push: one explicit ref, never forced, never deleting.
        refspecs = [a for a in args[1:] if not a.startswith("-")][1:]
        if any(a in _PUSH_FORBIDDEN_FLAGS for a in args[1:]) or len(refspecs) != 1:
            raise ForbiddenOperationError(f"git {' '.join(args)} is not permitted")
        if refspecs[0].startswith(("+", ":")) or refspecs[0].count(":") != 1:
            raise ForbiddenOperationError(f"refspec {refspecs[0]} is not permitted")
        return
    if sub in _FORBIDDEN_SUBCOMMANDS:
        raise ForbiddenOperationError(f"git {sub} is not permitted by the orchestrator")
    forbidden = _FORBIDDEN_FLAGS.get(sub, set())
    if any(a in forbidden for a in args[1:]):
        raise ForbiddenOperationError(f"git {' '.join(args)} is not permitted")
    # --force is only allowed to remove the orchestrator's *own* temporary worktree.
    if "--force" in args and args[:2] != ("worktree", "remove"):
        raise ForbiddenOperationError(f"git {' '.join(args)}: --force is not permitted")


T = TypeVar("T")

# Failures worth retrying: the network or the server, never the request itself
# (a rejected non-fast-forward push, an auth failure or a missing repo is final).
_TRANSIENT_MARKERS = (
    "could not resolve host",
    "connection timed out",
    "operation timed out",
    "failed to connect",
    "connection reset",
    "rpc failed",
    "early eof",
    "remote end hung up",
    "the requested url returned error: 5",
    "gnutls",
    "ssl",
)


def is_transient(error: GitError) -> bool:
    text = error.message.lower()
    return any(marker in text for marker in _TRANSIENT_MARKERS)


def retry(
    operation: Callable[[], T],
    *,
    attempts: int = 3,
    base_delay: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Run ``operation``; retry transient Git network errors with exponential backoff."""
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except GitError as exc:
            if attempt == attempts or not is_transient(exc):
                raise
            delay = base_delay * 2 ** (attempt - 1)
            log.warning(
                "transient git failure, retrying", extra={"attempt": attempt, "delay": delay}
            )
            sleep(delay)
    raise AssertionError("unreachable")


class Git:
    """Git operations bound to one repository or worktree directory."""

    def __init__(
        self, path: Path, *, network_attempts: int = 3, sleep: Callable[[float], None] = time.sleep
    ) -> None:
        self.path = Path(path)
        self.network_attempts = network_attempts
        self._sleep = sleep

    # ------------------------------------------------------------------ plumbing
    def run(
        self,
        *args: str,
        check: bool = True,
        stdin: str | None = None,
        as_bot: bool = False,
        bot_author: bool = False,
        allow_push: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        _guard(args, allow_push=allow_push)
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C", "LANG": "C"}
        if as_bot:
            # Release commits are *committed* by the orchestrator; cherry-picks keep the
            # original author so provenance stays visible in `git log`.
            env |= {"GIT_COMMITTER_NAME": BOT_NAME, "GIT_COMMITTER_EMAIL": BOT_EMAIL}
        if bot_author:
            env |= {"GIT_AUTHOR_NAME": BOT_NAME, "GIT_AUTHOR_EMAIL": BOT_EMAIL}
        cmd = ["git", "-c", "core.longpaths=true", "-C", str(self.path), *args]
        log.debug("git", extra={"argv": cmd[3:]})
        try:
            proc = subprocess.run(
                cmd,
                input=stdin,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                check=False,
                timeout=300,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GitError(f"Failed to run git {args[0]}: {exc}") from exc
        if check and proc.returncode != 0:
            raise GitError(
                f"git {args[0]} failed (exit {proc.returncode}): {proc.stderr.strip()[:500]}",
                details={"argv": list(args), "returncode": proc.returncode},
            )
        return proc

    def out(self, *args: str) -> str:
        return self.run(*args).stdout.strip()

    # ------------------------------------------------------------------ queries
    def is_repo(self) -> bool:
        return self.run("rev-parse", "--git-dir", check=False).returncode == 0

    def rev_parse(self, ref: str) -> str:
        proc = self.run("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
        if proc.returncode != 0:
            raise GitError(f"Cannot resolve revision '{ref}'", details={"ref": ref})
        return proc.stdout.strip()

    def try_rev_parse(self, ref: str) -> str | None:
        try:
            return self.rev_parse(ref)
        except GitError:
            return None

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        proc = self.run("merge-base", "--is-ancestor", ancestor, descendant, check=False)
        if proc.returncode not in (0, 1):
            raise GitError(f"merge-base failed: {proc.stderr.strip()}")
        return proc.returncode == 0

    def rev_list(self, range_spec: str, *, no_merges: bool = True) -> list[str]:
        args = ["rev-list", "--reverse", "--topo-order"]
        if no_merges:
            args.append("--no-merges")
        out = self.out(*args, range_spec)
        return out.split() if out else []

    def merge_commits(self, range_spec: str) -> list[str]:
        out = self.out("rev-list", "--merges", range_spec)
        return out.split() if out else []

    def commit_info(self, sha: str) -> CommitInfo:
        fmt = "%H%x00%P%x00%an%x00%ae%x00%s%x00%B"
        raw = self.run("show", "-s", f"--format={fmt}", sha).stdout
        full, parents, an, ae, subject, body = raw.split("\x00", 5)
        return CommitInfo(
            sha=full.strip(),
            parents=tuple(parents.split()),
            author_name=an,
            author_email=ae,
            subject=subject.strip(),
            body=body.strip(),
            files=self.changed_files(full.strip()),
        )

    def changed_files(self, sha: str) -> tuple[FileChange, ...]:
        out = self.out("diff-tree", "--root", "--no-commit-id", "--name-status", "-r", "-M", sha)
        files: list[FileChange] = []
        for line in out.splitlines():
            parts = line.split("\t")
            status = parts[0][:1]
            if status in {"R", "C"} and len(parts) == 3:
                files.append(FileChange(status=status, path=parts[2], old_path=parts[1]))
            elif len(parts) >= 2:
                files.append(FileChange(status=status, path=parts[1]))
        return tuple(files)

    def show_file(self, rev: str, path: str) -> str | None:
        proc = self.run("show", f"{rev}:{path}", check=False)
        return proc.stdout if proc.returncode == 0 else None

    def unified_diff(self, sha: str, *, context: int = 0) -> str:
        return self.out("show", "--format=", f"-U{context}", "--no-color", sha)

    def patch_id(self, sha: str) -> str:
        patch = self.run("show", "--no-color", sha).stdout
        out = self.run("patch-id", "--stable", stdin=patch).stdout.strip()
        return out.split()[0] if out else ""

    def tags(self, pattern: str) -> list[str]:
        out = self.out("tag", "--list", pattern)
        return out.splitlines() if out else []

    def branch_exists(self, name: str) -> bool:
        return self.try_rev_parse(f"refs/heads/{name}") is not None

    def check_branch_name(self, name: str) -> None:
        if self.run("check-ref-format", "--branch", name, check=False).returncode != 0:
            raise GitError(f"Invalid branch name '{name}'", details={"branch": name})

    def status_porcelain(self) -> str:
        return self.run("status", "--porcelain=v1", "--untracked-files=all").stdout

    def conflicted_files(self) -> list[str]:
        out = self.out("diff", "--name-only", "--diff-filter=U")
        return out.splitlines() if out else []

    # ------------------------------------------------------------------ mutations
    def worktree_add_detached(self, path: Path, rev: str) -> Git:
        self.run("worktree", "add", "--detach", str(path), rev)
        return Git(path)

    def worktree_remove(self, path: Path) -> None:
        self.run("worktree", "remove", "--force", str(path), check=False)
        self.run("worktree", "prune", check=False)

    def cherry_pick(self, sha: str) -> tuple[bool, list[str], str]:
        """Apply ``sha`` with provenance (-x). Returns (ok, conflicted_files, stderr)."""
        proc = self.run("cherry-pick", "-x", "--allow-empty", sha, check=False, as_bot=True)
        if proc.returncode == 0:
            return True, [], ""
        conflicts = self.conflicted_files()
        self.run("cherry-pick", "--abort", check=False)
        return False, conflicts, (proc.stderr or proc.stdout).strip()[:1000]

    def commit_all(self, message: str) -> str:
        """Commit all changes in this (temporary) worktree, authored by the orchestrator."""
        self.run("add", "--all")
        self.run("commit", "--no-verify", "-m", message, as_bot=True, bot_author=True)
        return self.rev_parse("HEAD")

    def remote_url(self, remote: str) -> str:
        return self.out("remote", "get-url", remote)

    def ls_remote(self, remote: str, *patterns: str) -> list[tuple[str, str]]:
        """``(sha, ref)`` pairs advertised by the remote (read-only; transient errors retried)."""
        out = retry(
            lambda: self.out("ls-remote", remote, *patterns),
            attempts=self.network_attempts,
            sleep=self._sleep,
        )
        pairs = []
        for line in out.splitlines():
            sha, _, name = line.partition(chr(9))  # ls-remote separates with a tab
            pairs.append((sha, name))
        return pairs

    def remote_ref(self, remote: str, ref: str) -> str | None:
        """SHA of ``ref`` on the remote, or None if absent."""
        return next((sha for sha, name in self.ls_remote(remote, ref) if name == ref), None)

    def push_ref(self, remote: str, ref: str, *, allowlist: tuple[str, ...]) -> dict[str, str]:
        """Push one ``refs/heads/*`` or ``refs/tags/*`` ref to an allowlisted remote.

        Never forced: Git rejects non-fast-forward branch updates, and an existing remote
        tag is never overwritten (we refuse before pushing if it points elsewhere).
        """
        from release_orchestrator.config import normalize_remote

        if not ref.startswith(("refs/heads/", "refs/tags/")):
            raise ForbiddenOperationError(f"Only branch or tag refs can be pushed, not {ref}")
        url = normalize_remote(self.remote_url(remote))
        if url not in allowlist:
            raise ForbiddenOperationError(
                f"Remote {url} is not in remote_allowlist; refusing to push",
                details={"remote": url, "allowlist": list(allowlist)},
            )
        local = self.rev_parse(ref)
        existing = self.remote_ref(remote, ref)
        if existing == local:
            return {"ref": ref, "remote": url, "sha": local, "status": "already_pushed"}
        if existing is not None and ref.startswith("refs/tags/"):
            raise ForbiddenOperationError(
                f"Tag {ref} already exists on the remote at another commit; tags are immutable",
                details={"remote_sha": existing, "local_sha": local},
            )

        def attempt() -> None:
            # A previous attempt may have landed even though its response was lost.
            if self.remote_ref(remote, ref) == local:
                return
            self.run("push", "--porcelain", remote, f"{ref}:{ref}", allow_push=True)

        retry(attempt, attempts=self.network_attempts, sleep=self._sleep)
        return {"ref": ref, "remote": url, "sha": local, "status": "pushed"}

    def create_branch(self, name: str, sha: str) -> None:
        """Create a new branch. Never moves an existing one (no -f)."""
        self.check_branch_name(name)
        if self.branch_exists(name):
            raise GitError(f"Branch '{name}' already exists", details={"branch": name})
        self.run("branch", name, sha)

    def archive_bytes(self, rev: str) -> bytes:
        """Deterministic tar of the committed tree at ``rev`` (no .git, no untracked files)."""
        cmd = [
            "git",
            "-c",
            "core.longpaths=true",
            "-C",
            str(self.path),
            "archive",
            "--format=tar",
            rev,
        ]
        proc = subprocess.run(cmd, capture_output=True, check=False, timeout=300)
        if proc.returncode != 0:
            raise GitError(f"git archive failed: {proc.stderr.decode(errors='replace')[:500]}")
        return proc.stdout

    def archive_to(self, rev: str, dest: Path) -> None:
        extract_tar(self.archive_bytes(rev), dest)


def extract_tar(data: bytes, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        tar.extractall(dest, filter="data")


def clone_no_checkout(source: Path, dest: Path) -> Git:
    """Read-only sandbox: clone into ``dest`` without touching ``source``."""
    proc = subprocess.run(
        [
            "git",
            "-c",
            "core.longpaths=true",
            "clone",
            "--quiet",
            "--no-checkout",
            "--no-hardlinks",
            str(source),
            str(dest),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if proc.returncode != 0:
        raise GitError(f"Sandbox clone failed: {proc.stderr.strip()[:500]}")
    return Git(dest)
