"""Consumer dependency update: rewrite the pin on an isolated branch and validate it
against the *published* artifact (never against an unpublished build)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from release_orchestrator.config import ConsumerConfig
from release_orchestrator.consumers.discovery import find_requirement, normalize
from release_orchestrator.errors import BlockedError
from release_orchestrator.fsutil import temp_dir
from release_orchestrator.gitutil.repo import Git, clone_no_checkout
from release_orchestrator.validation.runner import ValidationReport, run_validation


@dataclass
class ConsumerUpdateResult:
    consumer: str
    branch: str
    status: str  # prepared | already_prepared | dry_run_ok | validation_failed
    head: str | None
    from_version: str | None
    to_version: str
    validation: ValidationReport | None = None
    pull_request: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "consumer": self.consumer,
            "branch": self.branch,
            "status": self.status,
            "head": self.head,
            "from_version": self.from_version,
            "to_version": self.to_version,
            "validation": self.validation.to_dict() if self.validation else None,
            "pull_request": self.pull_request,
            "notes": self.notes,
        }


def branch_name(consumer: ConsumerConfig, library: str, version: str) -> str:
    return f"{consumer.branch_prefix}{normalize(library)}-{version}"


def rewrite_pin(text: str, library: str, version: str) -> str:
    req = find_requirement(text, library)
    if req is None or req.operator != "==":
        raise BlockedError(f"No '==' pin for {library} found", reason="PIN_NOT_FOUND")
    lines = text.splitlines(keepends=True)
    line = lines[req.line_no]
    match = re.search(r"(==\s*)[^\s;#,]+", line)
    if match is None:
        raise BlockedError(f"No '==' pin for {library} found", reason="PIN_NOT_FOUND")
    lines[req.line_no] = line[: match.end(1)] + version + line[match.end() :]
    return "".join(lines)


def prepare_consumer_update(
    consumer: ConsumerConfig,
    *,
    library: str,
    from_version: str | None,
    to_version: str,
    change_keys: list[str],
    library_dir: Path,
    dry_run: bool,
) -> ConsumerUpdateResult:
    git = Git(consumer.repo)
    branch = branch_name(consumer, library, to_version)
    git.check_branch_name(branch)
    result = ConsumerUpdateResult(consumer.name, branch, "prepared", None, from_version, to_version)

    if git.branch_exists(branch):
        text = git.show_file(branch, consumer.dependency_file) or ""
        req = find_requirement(text, library)
        if req is None or req.version != to_version:
            raise BlockedError(
                f"{consumer.name}: branch '{branch}' exists but does not pin {to_version}",
                reason="BRANCH_EXISTS_MISMATCH",
                details={"branch": branch},
            )
        result.status, result.head = "already_prepared", git.rev_parse(branch)
        result.validation = _validate(git, result.head, consumer, library_dir)
        return result

    base = git.rev_parse(consumer.source_ref)
    message = (
        f"build(deps): bump {library} from {from_version} to {to_version}\n\n"
        f"Refs: {', '.join(change_keys)}\n"
    )
    with temp_dir("lro-consumer-") as tmp:
        if dry_run:
            work = clone_no_checkout(git.path, tmp / "sandbox")
            work.run("checkout", "--quiet", "--detach", base)
        else:
            work = git.worktree_add_detached(tmp / "worktree", base)
        try:
            dep_file = work.path / consumer.dependency_file
            dep_file.write_text(
                rewrite_pin(dep_file.read_text(encoding="utf-8"), library, to_version),
                encoding="utf-8",
                newline="\n",
            )
            head = work.commit_all(message)
            result.validation = _validate(work, head, consumer, library_dir)
        finally:
            if not dry_run:
                git.worktree_remove(tmp / "worktree")
    result.head = head
    if dry_run:
        result.status = "dry_run_ok"
        return result
    if result.validation is None or not result.validation.passed:
        # Same rule as the library: a failed candidate leaves no branch behind.
        result.status = "validation_failed"
        result.head = None
        return result
    git.create_branch(branch, head)
    return result


def _validate(git: Git, rev: str, consumer: ConsumerConfig, library_dir: Path) -> ValidationReport:
    with temp_dir("lro-consumer-check-") as tmp:
        git.archive_to(rev, tmp)
        return run_validation(
            consumer.validation, scope=consumer.name, cwd=tmp, library_path=library_dir
        )
