"""Safe, deterministic Git access.

Every call uses an argv list (never a shell) and fails loudly with ``GitError``.
Destructive operations are refused by ``_guard`` so that no code path in the orchestrator
can force-push, merge, reset, or delete branches, even by mistake.
"""

from __future__ import annotations

import atexit
import io
import logging
import os
import re
import subprocess
import tarfile
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar

from release_orchestrator import cache as disk_cache
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


_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


@dataclass
class GitStats:
    """Process-wide Git cost counters (benchmarks and ``--timings``)."""

    calls: int = 0
    seconds: float = 0.0
    batch_reads: int = 0
    memo_hits: int = 0
    by_command: dict[str, int] = field(default_factory=dict)

    def reset(self) -> None:
        self.calls, self.seconds, self.batch_reads, self.memo_hits = 0, 0.0, 0, 0
        self.by_command = {}

    def snapshot(self) -> dict[str, object]:
        return {
            "subprocess_calls": self.calls,
            "subprocess_seconds": round(self.seconds, 3),
            "batch_object_reads": self.batch_reads,
            "memo_hits": self.memo_hits,
            "by_command": dict(sorted(self.by_command.items())),
        }


STATS = GitStats()


@dataclass
class NetworkStats:
    """Network / connector time: ``git ls-remote`` and GitHub REST calls."""

    calls: int = 0
    seconds: float = 0.0
    git_seconds: float = 0.0  # the part of STATS.seconds that was network (ls-remote)

    def reset(self) -> None:
        self.calls, self.seconds, self.git_seconds = 0, 0.0, 0.0


NETWORK = NetworkStats()

# Performance switch (ADR-0016). Every optimisation below is exact: reads are memoised only
# when they are addressed by a full commit SHA (immutable content), per repository path.
# ``LRO_OPTIMIZE=0`` (or ``set_optimizations(False)``) runs the original code paths; the
# differential tests compare both modes.
_OPTIMIZE = os.environ.get("LRO_OPTIMIZE", "1") != "0"
_MEMO: dict[tuple[str, ...], object] = {}


def set_optimizations(enabled: bool) -> None:
    global _OPTIMIZE
    _OPTIMIZE = enabled
    clear_memo()


def optimizations_enabled() -> bool:
    return _OPTIMIZE


def clear_memo() -> None:
    _MEMO.clear()
    close_readers()


_SNAPSHOT: set[str] | None = None  # repository paths whose refs are frozen for this run


@contextmanager
def read_snapshot(paths: Iterable[Path]) -> Iterator[None]:
    """Resolve each ref of ``paths`` at most once inside a **read-only** command.

    Refs are mutable, so they are never cached across runs; inside one read-only run the
    first resolution is reused, which also makes the run internally consistent. Only the
    user's repositories are listed: sandbox clones (whose HEAD moves) are never frozen.
    Never used around commands that write.
    """
    global _SNAPSHOT
    previous = _SNAPSHOT
    _SNAPSHOT = {str(Path(p).resolve()) for p in paths} if _OPTIMIZE else None
    try:
        yield
    finally:
        _SNAPSHOT = previous
        for key in [k for k in _MEMO if k[1] in ("ref", "tags")]:
            del _MEMO[key]


def _is_sha(value: str) -> bool:
    return bool(_FULL_SHA.match(value))


class _ReaderError(Exception):
    """The batch reader failed (dead process, broken pipe, protocol error). Callers fall
    back to the original ``git show`` path: a read failure is never a "missing file"."""


class _NotABlob(Exception):
    """The object exists but isn't a file (a tree, a submodule): use ``git show``."""


class _BatchReader:
    """One persistent ``git cat-file --batch`` for one repository, owned by a session."""

    def __init__(self, path: Path) -> None:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C", "LANG": "C"}
        _guard(("cat-file", "--batch"))
        try:
            self._proc = subprocess.Popen(
                ["git", "-c", "core.longpaths=true", "-C", str(path), "cat-file", "--batch"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=env,
            )
        except OSError as exc:
            raise _ReaderError(f"cannot start git cat-file: {exc}") from exc
        STATS.calls += 1
        STATS.by_command["cat-file --batch (process)"] = (
            STATS.by_command.get("cat-file --batch (process)", 0) + 1
        )

    @property
    def alive(self) -> bool:
        return self._proc.poll() is None

    def read(self, spec: str) -> bytes | None:
        """Blob content; ``None`` only when Git answers "missing" (exactly when ``git
        show rev:path`` fails). A non-blob raises ``_NotABlob``; anything else raises."""
        proc = self._proc
        if proc.stdin is None or proc.stdout is None or proc.poll() is not None:
            raise _ReaderError("cat-file process is not running")
        wanted = spec.encode("utf-8")
        start = time.perf_counter()
        try:
            proc.stdin.write(wanted + b"\n")
            proc.stdin.flush()
            line = proc.stdout.readline()
            if not line.endswith(b"\n"):
                raise _ReaderError("cat-file closed its output (EOF)")
            line = line[:-1]
            # "<spec> missing" / "<spec> ambiguous": the spec may itself contain spaces
            if line in (wanted + b" missing", wanted + b" ambiguous"):
                return None
            oid, kind, size_text = line.rsplit(b" ", 2)
            if not (_FULL_SHA.match(oid.decode("ascii", "replace")) and size_text.isdigit()):
                raise _ReaderError(f"unexpected cat-file header {line[:120]!r}")
            size = int(size_text)
            data = proc.stdout.read(size)
            if len(data) != size or proc.stdout.read(1) != b"\n":
                raise _ReaderError("cat-file returned a truncated object")
            if kind != b"blob":
                raise _NotABlob(kind.decode("ascii", "replace"))
            return data
        except (OSError, ValueError) as exc:
            raise _ReaderError(f"cat-file read failed: {exc}") from exc
        finally:
            STATS.batch_reads += 1
            STATS.seconds += time.perf_counter() - start

    def close(self) -> None:
        """Release the process and every pipe handle (needed on Windows to delete repos)."""
        proc = self._proc
        with suppress(Exception):
            if proc.stdin:
                proc.stdin.close()  # EOF: cat-file exits on its own
        try:
            proc.wait(timeout=5)
        except Exception:
            with suppress(Exception):
                proc.kill()
                proc.wait(timeout=5)
        with suppress(Exception):
            if proc.stdout:
                proc.stdout.close()


# Readers exist only inside ``git_session()``; at most MAX_READERS per session (LRU).
MAX_READERS = 8
_SESSION_DEPTH = 0


@contextmanager
def git_session() -> Iterator[None]:
    """Own the batch readers of one unit of work (a CLI command, a test).

    Every reader opened inside is closed on exit, including when an exception
    interrupts the work. Outside a session no reader is ever started (reads use the
    original ``git show`` path), so long-lived processes can't accumulate them.
    """
    global _SESSION_DEPTH
    _SESSION_DEPTH += 1
    try:
        yield
    finally:
        _SESSION_DEPTH -= 1
        if _SESSION_DEPTH == 0:
            close_readers()


def close_readers() -> None:
    while _READERS:
        _READERS.pop(next(iter(_READERS))).close()


def open_reader_count() -> int:
    return sum(1 for r in _READERS.values() if r.alive)


_READERS: dict[str, _BatchReader] = {}
atexit.register(close_readers)  # safety net only; sessions close readers deterministically


def _commit_to_json(c: CommitInfo) -> dict[str, object]:
    return {
        "sha": c.sha,
        "parents": list(c.parents),
        "an": c.author_name,
        "ae": c.author_email,
        "subject": c.subject,
        "body": c.body,
        "files": [[f.status, f.path, f.old_path] for f in c.files],
    }


def _commit_from_json(d: object) -> CommitInfo | None:
    try:
        assert isinstance(d, dict)  # noqa: S101
        return CommitInfo(
            sha=str(d["sha"]),
            parents=tuple(str(p) for p in d["parents"]),
            author_name=str(d["an"]),
            author_email=str(d["ae"]),
            subject=str(d["subject"]),
            body=str(d["body"]),
            files=tuple(
                FileChange(str(s), str(p), o if o is None else str(o)) for s, p, o in d["files"]
            ),
        )
    except (AssertionError, KeyError, TypeError, ValueError):
        return None  # corrupted entry: recompute


def _text(data: bytes) -> str:
    """Decode exactly like ``subprocess.run(text=True, encoding='utf-8', errors='replace')``
    (universal newlines), so batched reads equal ``git show`` output byte for byte."""
    return data.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")


class Git:
    """Git operations bound to one repository or worktree directory."""

    def __init__(
        self, path: Path, *, network_attempts: int = 3, sleep: Callable[[float], None] = time.sleep
    ) -> None:
        self.path = Path(path)
        self.network_attempts = network_attempts
        self._sleep = sleep
        self._key = str(self.path.resolve())

    def _memo(self, op: str, *args: str) -> tuple[tuple[str, ...], object | None]:
        key = (self._key, op, *args)
        value = _MEMO.get(key)
        if value is not None:
            STATS.memo_hits += 1
        return key, value

    def _immutable(self, op: str, args: tuple[str, ...], compute: Callable[[], T]) -> T:
        """Memo + disk cache for a Git query whose answer is fixed by full SHAs."""
        key = (self._key, op, *args)
        if key in _MEMO:
            STATS.memo_hits += 1
            return _MEMO[key]  # type: ignore[return-value]
        stored = disk_cache.active().get("git", op, *args)
        if stored is not None:
            _MEMO[key] = stored
            return stored  # type: ignore[no-any-return]
        value = compute()
        _MEMO[key] = value
        disk_cache.active().put("git", value, op, *args)
        return value

    def _reader(self) -> _BatchReader | None:
        """The session's reader for this repository (``None`` outside a session)."""
        if _SESSION_DEPTH == 0:
            return None
        reader = _READERS.pop(self._key, None)
        if reader is None or not reader.alive:
            if reader is not None:
                reader.close()
            reader = _BatchReader(self.path)
            while len(_READERS) >= MAX_READERS:  # evict the least recently used
                _READERS.pop(next(iter(_READERS))).close()
        _READERS[self._key] = reader  # most recently used last
        return reader

    def _drop_reader(self) -> None:
        reader = _READERS.pop(self._key, None)
        if reader is not None:
            reader.close()

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
        STATS.calls += 1
        STATS.by_command[args[0]] = STATS.by_command.get(args[0], 0) + 1
        started = time.perf_counter()
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
        finally:
            STATS.seconds += time.perf_counter() - started
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
        if _OPTIMIZE and _is_sha(ref):  # a full SHA resolves to itself (or fails) for ever
            key, hit = self._memo("sha", ref)
            if hit is None:
                hit = _MEMO[key] = self._rev_parse(ref)
            assert isinstance(hit, str)  # noqa: S101
            return hit
        if _SNAPSHOT is not None and self._key in _SNAPSHOT:
            key, hit = self._memo("ref", ref)
            if hit is None:
                hit = _MEMO[key] = self._rev_parse(ref)
            assert isinstance(hit, str)  # noqa: S101
            return hit
        return self._rev_parse(ref)

    def _rev_parse(self, ref: str) -> str:
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
        if _OPTIMIZE and _is_sha(ancestor) and _is_sha(descendant):  # immutable relation
            return bool(
                self._immutable(
                    "ancestor",
                    (ancestor, descendant),
                    lambda: self._is_ancestor(ancestor, descendant),
                )
            )
        return self._is_ancestor(ancestor, descendant)

    def _is_ancestor(self, ancestor: str, descendant: str) -> bool:
        proc = self.run("merge-base", "--is-ancestor", ancestor, descendant, check=False)
        if proc.returncode not in (0, 1):
            raise GitError(f"merge-base failed: {proc.stderr.strip()}")
        return proc.returncode == 0

    def rev_list(self, range_spec: str, *, no_merges: bool = True) -> list[str]:
        a, sep, b = range_spec.partition("..")
        if _OPTIMIZE and sep and _is_sha(a) and _is_sha(b):  # a SHA range is immutable
            return list(
                self._immutable(
                    "rev_list",
                    (range_spec, str(no_merges)),
                    lambda: self._rev_list(range_spec, no_merges=no_merges),
                )
            )
        return self._rev_list(range_spec, no_merges=no_merges)

    def _rev_list(self, range_spec: str, *, no_merges: bool = True) -> list[str]:
        args = ["rev-list", "--reverse", "--topo-order"]
        if no_merges:
            args.append("--no-merges")
        out = self.out(*args, range_spec)
        return out.split() if out else []

    def merge_commits(self, range_spec: str) -> list[str]:
        a, sep, b = range_spec.partition("..")
        if _OPTIMIZE and sep and _is_sha(a) and _is_sha(b):
            return list(
                self._immutable("merges", (range_spec,), lambda: self._merge_commits(range_spec))
            )
        return self._merge_commits(range_spec)

    def _merge_commits(self, range_spec: str) -> list[str]:
        out = self.out("rev-list", "--merges", range_spec)
        return out.split() if out else []

    def first_commit_matching(self, rev: str, pattern: str) -> str:
        """``git log rev -n1 --format=%H -E --grep=pattern`` ("" if none); cached for a SHA."""

        def compute() -> str:
            return self.out("log", rev, "-n", "1", "--format=%H", "-E", f"--grep={pattern}")

        if _OPTIMIZE and _is_sha(rev):
            return str(self._immutable("grep", (rev, pattern), compute))
        return compute()

    def commit_info(self, sha: str) -> CommitInfo:
        if _OPTIMIZE and _is_sha(sha):
            key, hit = self._memo("commit_info", sha)
            if hit is None:
                stored = disk_cache.active().get("commit", sha)
                hit = _commit_from_json(stored) if stored is not None else None
                if hit is None:
                    hit = self._commit_info(sha)
                    disk_cache.active().put("commit", _commit_to_json(hit), sha)
                _MEMO[key] = hit
            assert isinstance(hit, CommitInfo)  # noqa: S101
            return hit
        return self._commit_info(sha)

    def _commit_info(self, sha: str) -> CommitInfo:
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
        if _OPTIMIZE and _is_sha(rev) and "\n" not in path:
            key, hit = self._memo("show_file", rev, path)
            if hit is None:
                try:
                    reader = self._reader()
                    if reader is None:  # no session: original path, still memoised
                        content = self._show_file(rev, path)
                        hit = ("", content) if content is not None else ("missing",)
                    else:
                        data = reader.read(f"{rev}:{path}")
                        hit = ("", _text(data)) if data is not None else ("missing",)
                except _NotABlob:  # e.g. a directory: git show prints a listing
                    content = self._show_file(rev, path)
                    hit = ("", content) if content is not None else ("missing",)
                except _ReaderError as exc:
                    # A failed read is never "missing": drop the reader and answer this
                    # read through the original subprocess path (not memoised).
                    log.warning("cat-file reader failed; using git show", extra={"note": str(exc)})
                    self._drop_reader()
                    return self._show_file(rev, path)
                _MEMO[key] = hit
            assert isinstance(hit, tuple)  # noqa: S101
            return hit[1] if len(hit) == 2 else None
        return self._show_file(rev, path)

    def _show_file(self, rev: str, path: str) -> str | None:
        proc = self.run("show", f"{rev}:{path}", check=False)
        return proc.stdout if proc.returncode == 0 else None

    def ls_files_at(self, rev: str) -> list[str]:
        """``git ls-tree -r --name-only rev`` (memoised for a full commit SHA)."""
        if _OPTIMIZE and _is_sha(rev):
            listing = self._immutable(
                "ls_tree", (rev,), lambda: self.out("ls-tree", "-r", "--name-only", rev)
            )
            return str(listing).splitlines()
        return self.out("ls-tree", "-r", "--name-only", rev).splitlines()

    def unified_diff(self, sha: str, *, context: int = 0) -> str:
        if _OPTIMIZE and _is_sha(sha):
            key, hit = self._memo("diff", sha, str(context))
            if hit is None:
                hit = _MEMO[key] = self.out("show", "--format=", f"-U{context}", "--no-color", sha)
            assert isinstance(hit, str)  # noqa: S101
            return hit
        return self.out("show", "--format=", f"-U{context}", "--no-color", sha)

    def patch_id(self, sha: str) -> str:
        """``git show | git patch-id --stable`` for any commit, merges included."""
        if _OPTIMIZE and _is_sha(sha):
            key, hit = self._memo("patch_id", sha)
            if isinstance(hit, str):
                return hit
            stored = disk_cache.active().get("patch_id", sha)
            value = stored if isinstance(stored, str) else self._patch_id(sha)
            if not isinstance(stored, str):
                disk_cache.active().put("patch_id", value, sha)
            _MEMO[key] = value
            return value
        return self._patch_id(sha)

    def _patch_id(self, sha: str) -> str:
        patch = self.run("show", "--no-color", sha).stdout
        out = self.run("patch-id", "--stable", stdin=patch).stdout.strip()
        return out.split()[0] if out else ""

    def patch_ids(self, shas: list[str]) -> dict[str, str]:
        """Patch-ids of many **non-merge** commits in two subprocesses (ADR-0016).

        ``git log --no-walk=unsorted -p`` emits exactly what ``git show`` emits per commit,
        and one ``git patch-id --stable`` reads the whole stream. Commits without a patch
        (empty) get "" like ``patch_id``. Falls back to per-commit calls when disabled.
        """
        if not shas:
            return {}
        if not _OPTIMIZE or not all(_is_sha(sha) for sha in shas):
            return {sha: self._patch_id(sha) for sha in shas}  # refs are never memoised
        result: dict[str, str] = {}
        missing = []
        for sha in shas:
            key, hit = self._memo("patch_id", sha)
            if not isinstance(hit, str):
                stored = disk_cache.active().get("patch_id", sha)
                if isinstance(stored, str):
                    hit = _MEMO[key] = stored
            if isinstance(hit, str):
                result[sha] = hit
            else:
                missing.append(sha)
        if missing:
            log_out = self.run(
                "log",
                "--no-walk=unsorted",
                "--no-color",
                "--no-decorate",
                "-p",
                "--stdin",
                stdin="\n".join(missing) + "\n",
            ).stdout
            ids = self.run("patch-id", "--stable", stdin=log_out).stdout
            found = {}
            for line in ids.splitlines():
                parts = line.split()
                if len(parts) == 2:
                    found[parts[1]] = parts[0]
            for sha in missing:
                result[sha] = _MEMO[(self._key, "patch_id", sha)] = found.get(sha, "")
                disk_cache.active().put("patch_id", result[sha], sha)
        return result

    def tags(self, pattern: str) -> list[str]:
        if _SNAPSHOT is not None and self._key in _SNAPSHOT:
            key, hit = self._memo("tags", pattern)
            if hit is None:
                hit = _MEMO[key] = self.out("tag", "--list", pattern)
            assert isinstance(hit, str)  # noqa: S101
            return hit.splitlines() if hit else []
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
        started = time.perf_counter()
        try:
            out = retry(
                lambda: self.out("ls-remote", remote, *patterns),
                attempts=self.network_attempts,
                sleep=self._sleep,
            )
        finally:
            elapsed = time.perf_counter() - started
            NETWORK.calls += 1
            NETWORK.seconds += elapsed
            NETWORK.git_seconds += elapsed
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
    STATS.calls += 1
    STATS.by_command["clone"] = STATS.by_command.get("clone", 0) + 1
    started = time.perf_counter()
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
    STATS.seconds += time.perf_counter() - started
    if proc.returncode != 0:
        raise GitError(f"Sandbox clone failed: {proc.stderr.strip()[:500]}")
    return Git(dest)
