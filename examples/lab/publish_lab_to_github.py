"""Seed the GitHub sandbox repositories with the generated release lab.

    python examples/lab/publish_lab_to_github.py --dest <empty dir> [--owner zaidmoen] --yes

1. Generates the lab locally (`lab-init`, PR provider `external`).
2. Adds `origin` = https://github.com/<owner>/<repo> to each of the three repositories.
3. Pushes `main` and the lab tags (v1.0.0, v1.1.0-rc.22, v1.1.0-rc.23), one ref at a time,
   never forced. If a ref already exists on the remote at the same commit, it is skipped.
   If it exists at a different commit, the script stops (it never overwrites).

The three repositories must already exist (empty, private) and be accessible with your Git
credentials. Repository creation is a manual, human step (see docs/lab-setup.md).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPOS = ("harri-release-lab-library", "harri-release-lab-payment", "harri-release-lab-invoice")
ALLOWED_OWNERS = {"zaidmoen"}
LIB_TAGS = ("v1.0.0", "v1.1.0-rc.22", "v1.1.0-rc.23")


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False, env=None
    )
    if proc.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed in {repo.name}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def push_ref(repo: Path, ref: str) -> str:
    local = git(repo, "rev-parse", ref)
    remote = git(repo, "ls-remote", "origin", ref)
    remote_sha = remote.split()[0] if remote else None
    if remote_sha == local:
        return f"{repo.name} {ref}: already on remote"
    if remote_sha is not None:
        raise SystemExit(f"{repo.name} {ref} exists on the remote at {remote_sha}; not overwriting")
    git(repo, "push", "origin", f"{ref}:{ref}")
    return f"{repo.name} {ref}: pushed {local[:10]}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dest", type=Path, required=True)
    parser.add_argument("--owner", default="zaidmoen")
    parser.add_argument("--yes", action="store_true", help="confirm the remote writes")
    args = parser.parse_args()
    if args.owner not in ALLOWED_OWNERS:
        raise SystemExit(f"owner {args.owner!r} is not in the sandbox allowlist {ALLOWED_OWNERS}")
    if not args.yes:
        raise SystemExit(
            "This pushes to GitHub. Re-run with --yes after reviewing the target repos."
        )

    from release_orchestrator.lab import build_lab

    config = build_lab(args.dest, provider="external")
    print(f"lab generated: {config}")
    for name in REPOS:
        repo = args.dest / name
        git(repo, "remote", "add", "origin", f"https://github.com/{args.owner}/{name}.git")
        print(push_ref(repo, "refs/heads/main"))
        if name == REPOS[0]:
            for tag in LIB_TAGS:
                print(push_ref(repo, f"refs/tags/{tag}"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
