#!/usr/bin/env python3
"""Install and verify a pinned Library Release Orchestrator engine (ADR-0018).

Standard library only: it runs with the engineer's system Python, before any dependency
exists. The distributed ``SKILL.md`` fetches exactly one commit (a full SHA) into a
temporary directory and checks ``HEAD`` itself, then runs *this file from that verified
checkout*:

    python <tmp>/scripts/lro_engine.py install --sha <SHA> --checkout <tmp>
    python <home>/engines/<SHA>/src/scripts/lro_engine.py verify --sha <SHA>
    python <any engine>/scripts/lro_engine.py status

Layout (user level, never inside the target repository)::

    $LRO_HOME (default ~/.lro)
      engines/<sha>/src/        the verified checkout (detached, clean)
      engines/<sha>/venv/       isolated virtual environment (runtime deps only)
      engines/<sha>/bin/lro     wrapper: runs the engine with absolute paths, same cwd
      engines/<sha>/engine.json written last; its presence means "verified install"
      locks/<sha>.lock          one installer at a time per revision

Nothing here downloads code. It never executes the checkout before verifying it, never
updates an installed engine, and never touches the current working directory.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from typing import Any

MIN_PYTHON = (3, 11)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
LOCK_STALE_SECONDS = 15 * 60
ALLOWED_ORIGINS = ("https://github.com/zaidmoen/library-release-orchestrator.git",)
SKILL_ENTRY = Path(".claude/skills/library-release/scripts/lro.py")


class InstallError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def lro_home() -> Path:
    env = os.environ.get("LRO_HOME")
    return Path(env).expanduser().resolve() if env else (Path.home() / ".lro").resolve()


def _git(repo: Path, *args: str) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
        )
    except FileNotFoundError as exc:
        raise InstallError("GIT_MISSING", "git is not installed or not on PATH") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip()[:300]
        raise InstallError("GIT_FAILED", f"git {args[0]} failed: {detail}") from exc
    return out.stdout.strip()


def venv_python(venv: Path) -> Path:
    """Windows venvs use Scripts/python.exe; Linux and macOS use bin/python."""
    win = venv / "Scripts" / "python.exe"
    return win if os.name == "nt" or win.exists() else venv / "bin" / "python"


def check_python() -> None:
    if sys.version_info < MIN_PYTHON:
        raise InstallError(
            "PYTHON_TOO_OLD",
            f"Python {'.'.join(map(str, MIN_PYTHON))}+ is required, found {sys.version.split()[0]}",
        )


def verify_checkout(checkout: Path, sha: str, *, origins: tuple[str, ...] | None = None) -> str:
    """Return the tree SHA if ``checkout`` is exactly ``sha``, clean, from an allowed origin."""
    if not SHA_RE.match(sha):
        raise InstallError("BAD_SHA", "the engine revision must be a full 40-hex commit SHA")
    head = _git(checkout, "rev-parse", "HEAD")
    if head != sha:
        raise InstallError("REVISION_MISMATCH", f"checkout is at {head}, expected {sha}")
    if _git(checkout, "status", "--porcelain", "--untracked-files=all"):
        raise InstallError("CHECKOUT_MODIFIED", "the engine checkout has local modifications")
    allowed = origins if origins is not None else _allowed_origins()
    origin = _git(checkout, "config", "--get", "remote.origin.url")
    if origin not in allowed:
        raise InstallError("UNTRUSTED_ORIGIN", f"origin {origin!r} is not an approved source")
    if not (checkout / SKILL_ENTRY).is_file():
        raise InstallError("NOT_AN_ENGINE", f"{SKILL_ENTRY} is missing at {sha}")
    return _git(checkout, "rev-parse", "HEAD^{tree}")


def _allowed_origins() -> tuple[str, ...]:
    # Tests and air-gapped mirrors may add origins; the SHA stays the trust anchor.
    extra = os.environ.get("LRO_ALLOWED_ORIGINS", "")
    return ALLOWED_ORIGINS + tuple(o for o in extra.split(",") if o)


class Lock:
    """Cross-platform exclusive lock: O_CREAT|O_EXCL on a file; stale locks are broken."""

    def __init__(self, path: Path, timeout: float = 300.0) -> None:
        self.path, self.timeout = path, timeout

    def __enter__(self) -> Lock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                except FileNotFoundError:
                    continue
                if age > LOCK_STALE_SECONDS:
                    self.path.unlink(missing_ok=True)
                    continue
                if time.monotonic() > deadline:
                    raise InstallError(
                        "LOCK_TIMEOUT", f"another installation holds {self.path}"
                    ) from None
                time.sleep(0.2)
                continue
            with os.fdopen(fd, "w") as handle:
                handle.write(f"{os.getpid()} {time.time()}\n")
            return self

    def __exit__(self, *_: object) -> None:
        self.path.unlink(missing_ok=True)


def _rmtree(path: Path) -> None:
    """Git marks pack files read-only on Windows; make them writable, then remove."""

    def _retry(func: Any, target: str, _exc: Any) -> None:
        os.chmod(target, 0o700)
        func(target)

    if not path.exists():
        return
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_retry)
    else:
        shutil.rmtree(path, onerror=_retry)


def runtime_dependencies(checkout: Path) -> list[str]:
    data = tomllib.loads((checkout / "pyproject.toml").read_text(encoding="utf-8"))
    deps = data.get("project", {}).get("dependencies", [])
    return [str(d) for d in deps]


def _write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _wrapper(engine_python: Path, entry: Path) -> str:
    # Absolute paths only; no `cd`: the engineer's working directory is preserved.
    return (
        "#!/usr/bin/env bash\n"
        "# Generated by lro_engine.py: runs the pinned engine. Do not edit.\n"
        f'exec "{engine_python.as_posix()}" -I "{entry.as_posix()}" "$@"\n'
    )


def read_install(home: Path, sha: str) -> dict[str, Any] | None:
    marker = home / "engines" / sha / "engine.json"
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if data.get("sha") != sha or not Path(data.get("python", "")).is_file():
        return None
    return dict(data)


def install(
    sha: str,
    checkout: Path,
    *,
    home: Path | None = None,
    origins: tuple[str, ...] | None = None,
    pip_install: Any = None,
) -> dict[str, Any]:
    check_python()
    home = home or lro_home()
    checkout = checkout.resolve()
    tree = verify_checkout(checkout, sha, origins=origins)
    engine = home / "engines" / sha
    with Lock(home / "locks" / f"{sha}.lock"):
        existing = read_install(home, sha)
        if existing:
            if checkout != (engine / "src").resolve():
                _rmtree(checkout)  # a concurrent install won
            return {**existing, "status": "reused"}
        engine.mkdir(parents=True, exist_ok=True)
        src = engine / "src"
        if src.exists() and src.resolve() != checkout:
            _rmtree(src)  # an interrupted earlier attempt (no engine.json was written)
        if src.resolve() != checkout:
            os.replace(checkout, src)
        verify_checkout(src, sha, origins=origins)  # re-verify after the move
        venv = engine / "venv"
        if venv.exists():
            _rmtree(venv)
        deps = runtime_dependencies(src)
        no_pip = [] if deps else ["--without-pip"]
        subprocess.run([sys.executable, "-m", "venv", *no_pip, str(venv)], check=True)
        python = venv_python(venv)
        if deps:
            (pip_install or _pip_install)(python, deps)
        bin_dir = engine / "bin"
        bin_dir.mkdir(exist_ok=True)
        wrapper = bin_dir / "lro"
        wrapper.write_text(_wrapper(python, src / SKILL_ENTRY), encoding="utf-8", newline="\n")
        wrapper.chmod(0o755)
        data = {
            "sha": sha,
            "tree": tree,
            "python": str(python),
            "lro": str(wrapper),
            "skill_dir": str(src / ".claude" / "skills" / "library-release"),
            "dependencies": deps,
            "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        _write_json(engine / "engine.json", data)  # last: marks the install as verified
        return {**data, "status": "installed"}


def _pip_install(python: Path, deps: list[str]) -> None:
    proc = subprocess.run(
        [str(python), "-m", "pip", "install", "--disable-pip-version-check", "--no-input", *deps],
        capture_output=True,
        text=True,
    )
    if proc.returncode:
        raise InstallError("DEPENDENCIES_FAILED", proc.stderr.strip()[-500:])


def verify(sha: str, *, home: Path | None = None) -> dict[str, Any]:
    home = home or lro_home()
    data = read_install(home, sha)
    if data is None:
        raise InstallError("NOT_INSTALLED", f"engine {sha[:12]} is not installed in {home}")
    tree = verify_checkout(home / "engines" / sha / "src", sha)
    if tree != data["tree"]:
        raise InstallError("TREE_MISMATCH", "the installed engine no longer matches its revision")
    return {**data, "status": "verified"}


def status(home: Path | None = None) -> dict[str, Any]:
    home = home or lro_home()
    engines = []
    for path in sorted((home / "engines").glob("*")):
        data = read_install(home, path.name)
        engines.append({"sha": path.name, "installed": data is not None})
    return {"home": str(home), "engines": engines}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="lro_engine")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("install")
    p.add_argument("--sha", required=True)
    p.add_argument("--checkout", required=True, type=Path)
    v = sub.add_parser("verify")
    v.add_argument("--sha", required=True)
    sub.add_parser("status")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "install":
            result = install(args.sha, args.checkout)
        elif args.cmd == "verify":
            result = verify(args.sha)
        else:
            result = status()
    except InstallError as exc:
        print(json.dumps({"ok": False, "error": {"code": exc.code, "message": str(exc)}}))
        return 2
    except subprocess.CalledProcessError as exc:
        print(json.dumps({"ok": False, "error": {"code": "SETUP_FAILED", "message": str(exc)}}))
        return 6
    print(json.dumps({"ok": True, "result": result}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
