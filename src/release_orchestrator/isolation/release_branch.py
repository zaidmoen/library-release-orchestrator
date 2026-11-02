"""Change isolation engine: build a release branch from an approved plan.

The branch is assembled in a *detached* temporary worktree (or, for dry-run, a throwaway
clone) and only created as a ref after every commit applied, the version commit was made
and the result verified against the plan. A failure therefore never leaves a branch
behind, and the user's own checkout is never touched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from release_orchestrator.errors import BlockedError
from release_orchestrator.fsutil import temp_dir
from release_orchestrator.gitutil.repo import Git, clone_no_checkout
from release_orchestrator.planning.manifest import verify_release_branch, version_commit_message
from release_orchestrator.planning.plan import ReleasePlan


@dataclass
class PrepareResult:
    status: str  # prepared | already_prepared | dry_run_ok
    branch: str
    head: str | None
    dry_run: bool
    provenance: list[dict[str, str]] = field(default_factory=list)
    verification: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "branch": self.branch,
            "head": self.head,
            "dry_run": self.dry_run,
            "provenance": self.provenance,
            "verification": self.verification,
        }


def prepare_release(
    git: Git, plan: ReleasePlan, *, version_pattern: str, dry_run: bool
) -> PrepareResult:
    if plan.status != "PLAN_READY":
        raise BlockedError(
            "Plan is BLOCKED; it cannot be prepared",
            reason="PLAN_BLOCKED",
            details={"blockers": plan.blockers},
        )
    git.check_branch_name(plan.branch)

    if git.branch_exists(plan.branch):
        report = verify_release_branch(git, plan)
        if report.ok:
            return PrepareResult(
                "already_prepared",
                plan.branch,
                report.head,
                dry_run,
                report.provenance,
                report.to_dict(),
            )
        raise BlockedError(
            f"Branch '{plan.branch}' already exists with different content; it was not modified",
            reason="BRANCH_EXISTS_MISMATCH",
            details=report.to_dict(),
        )

    with temp_dir("lro-prepare-") as tmp:
        if dry_run:
            work = clone_no_checkout(git.path, tmp / "sandbox")
            work.run("checkout", "--quiet", "--detach", plan.base_revision)
            head = _build(work, plan, version_pattern)
            report = verify_release_branch(work, plan, ref=head)
            return PrepareResult(
                "dry_run_ok" if report.ok else "dry_run_mismatch",
                plan.branch,
                head,
                True,
                report.provenance,
                report.to_dict(),
            )

        worktree = tmp / "worktree"
        work = git.worktree_add_detached(worktree, plan.base_revision)
        try:
            head = _build(work, plan, version_pattern)
        finally:
            git.worktree_remove(worktree)

    # Objects are shared with the main repository, so the result can be verified before a
    # ref ever points at it.
    report = verify_release_branch(git, plan, ref=head)
    if not report.ok:
        raise BlockedError(
            "Prepared commits do not match the plan; no branch was created",
            reason="MANIFEST_MISMATCH",
            details=report.to_dict(),
        )
    git.create_branch(plan.branch, head)
    report.branch = plan.branch
    return PrepareResult("prepared", plan.branch, head, False, report.provenance, report.to_dict())


def _build(work: Git, plan: ReleasePlan, version_pattern: str) -> str:
    for commit in plan.included_commits:
        ok, conflicts, message = work.cherry_pick(commit.sha)
        if not ok:
            raise BlockedError(
                f"Commit {commit.sha[:10]} does not apply cleanly; release branch not created",
                reason="GIT_CONFLICT",
                details={"commit": commit.sha, "conflicted_files": conflicts, "git": message},
            )
    if plan.version_file:
        _write_version(work.path / plan.version_file, version_pattern, plan.proposed_version)
        return work.commit_all(version_commit_message(plan))
    return work.rev_parse("HEAD")


def _write_version(path: Path, pattern: str, version: str) -> None:
    if not path.is_file():
        raise BlockedError(
            f"Version file {path.name} not found on the release base", reason="VERSION_FILE_MISSING"
        )
    text = path.read_text(encoding="utf-8")
    match = re.search(pattern, text)
    if not match:
        raise BlockedError(
            "Version pattern not found in version file",
            reason="VERSION_PATTERN_NOT_FOUND",
            details={"pattern": pattern},
        )
    start, end = match.span(1)
    path.write_text(text[:start] + version + text[end:], encoding="utf-8", newline="\n")
