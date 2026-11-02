"""Manifest schema validation and verification of a release branch against its plan.

A manifest's claims are never taken as proof: ``verify_release_branch`` re-derives the
actual release content from Git (ancestry, cherry-pick provenance, patch-ids, version
commit) and compares it with the approved plan.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from release_orchestrator.config import CommandSpec, Config
from release_orchestrator.errors import ManifestError
from release_orchestrator.gitutil.repo import Git
from release_orchestrator.planning.plan import (
    PLAN_STATUSES,
    SCHEMA_VERSION,
    ReleasePlan,
    compute_plan_id,
)
from release_orchestrator.versioning.semver import SemVer

_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_PLAN_ID = re.compile(r"^[0-9a-f]{16}$")
_BRANCH = re.compile(r"^(?!/)(?!.*\.\.)(?!.*//)[A-Za-z0-9._/-]+(?<![/.])$")
_CONSUMER_ACTIONS = {"update", "up_to_date", "manual", "not_targeted", "not_a_consumer"}
_PICKED_RE = re.compile(r"\(cherry picked from commit ([0-9a-f]{40})\)")


def validate_manifest(data: Any) -> list[str]:
    """Structural and semantic schema checks. Returns a list of human-readable errors."""
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["manifest root must be an object"]
    if data.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    for key in (
        "plan_id",
        "status",
        "library",
        "request",
        "release",
        "included_changes",
        "excluded_changes",
        "approval",
        "validation",
    ):
        if key not in data:
            errors.append(f"missing required field '{key}'")
    if errors:
        return errors

    if not _PLAN_ID.match(str(data["plan_id"])):
        errors.append("plan_id must be 16 lowercase hex characters")
    if data["status"] not in PLAN_STATUSES:
        errors.append(f"status must be one of {PLAN_STATUSES}")

    release = data["release"]
    for key in ("base_revision", "source_revision"):
        if not _SHA40.match(str(release.get(key, ""))):
            errors.append(f"release.{key} must be a full 40-character SHA")
    if SemVer.try_parse(str(release.get("proposed_version", ""))) is None:
        errors.append("release.proposed_version must be a valid semantic version")
    if release.get("status") != "provisional":
        errors.append("release.status must be 'provisional' (plans never claim publication)")
    if not _BRANCH.match(str(release.get("branch", ""))):
        errors.append("release.branch is not a safe branch name")

    included = data["included_changes"]
    excluded = data["excluded_changes"]
    if not isinstance(included, list) or not isinstance(excluded, list):
        return [*errors, "included_changes and excluded_changes must be lists"]
    inc_keys = [c.get("key") for c in included]
    exc_keys = [c.get("key") for c in excluded]
    if len(set(inc_keys)) != len(inc_keys):
        errors.append("included_changes contains duplicate keys")
    overlap = set(inc_keys) & set(exc_keys)
    if overlap:
        errors.append(f"changes both included and excluded: {sorted(overlap)}")
    shas = [m.get("sha", "") for c in included for m in c.get("commits", [])]
    if len(set(shas)) != len(shas):
        errors.append("a commit appears more than once in included_changes")
    if sorted(release.get("apply_order", [])) != sorted(shas):
        errors.append("release.apply_order must list exactly the included commits")
    for sha in shas:
        if not _SHA40.match(str(sha)):
            errors.append(f"included commit '{sha}' is not a full SHA")
    if data["status"] == "PLAN_READY":
        if not included:
            errors.append("a PLAN_READY plan must include at least one change")
        if data.get("blockers"):
            errors.append("a PLAN_READY plan must not have blockers")

    if data["approval"].get("required") is not True:
        errors.append("approval.required must be true")
    for req in data["validation"].get("required", []):
        argv = req.get("argv")
        if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
            errors.append(f"validation entry {req.get('name')!r} has an invalid argv")
    for consumer in data.get("consumers", []):
        if consumer.get("action") not in _CONSUMER_ACTIONS:
            errors.append(f"consumer {consumer.get('name')!r} has invalid action")

    if not errors:
        plan = ReleasePlan.from_dict(data)
        if compute_plan_id(plan.identity()) != data["plan_id"]:
            errors.append("plan_id does not match the plan content (manifest was modified)")
    return errors


def load_plan(path: Path, config: Config) -> ReleasePlan:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"Cannot read plan {path}: {exc}") from exc
    errors = validate_manifest(data)
    if errors:
        raise ManifestError("Release manifest is invalid", details={"errors": errors})
    plan = ReleasePlan.from_dict(data)
    check_plan_against_config(plan, config)
    return plan


def check_plan_against_config(plan: ReleasePlan, config: Config) -> None:
    """The plan file is not trusted to define *what runs*: commands come from config only."""
    problems = []
    if plan.library != config.library.name:
        problems.append(f"plan is for library '{plan.library}', config is '{config.library.name}'")
    expected = expected_validation(
        config, [c["name"] for c in plan.consumers if c["action"] == "update"]
    )
    if plan.validation_required != expected:
        problems.append("validation commands in the plan differ from the trusted config")
    if problems:
        raise ManifestError(
            "Plan does not match the trusted configuration", details={"errors": problems}
        )


def expected_validation(config: Config, consumer_names: list[str]) -> list[dict[str, Any]]:
    required = [_req("library", c) for c in config.library_validation]
    for consumer in config.consumers:
        if consumer.name in consumer_names:
            required += [_req(consumer.name, c) for c in consumer.validation]
    return required


def _req(scope: str, spec: CommandSpec) -> dict[str, Any]:
    return {
        "scope": scope,
        "name": spec.name,
        "argv": list(spec.argv),
        "env": dict(spec.env),
        "timeout_seconds": spec.timeout_seconds,
    }


@dataclass
class VerificationReport:
    ok: bool
    branch: str
    head: str | None
    checks: list[dict[str, Any]] = field(default_factory=list)
    provenance: list[dict[str, str]] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append({"check": name, "ok": ok, "detail": detail})
        if not ok:
            self.ok = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "branch": self.branch,
            "head": self.head,
            "checks": self.checks,
            "provenance": self.provenance,
        }


def version_commit_message(plan: ReleasePlan) -> str:
    keys = ", ".join(plan.included_keys)
    return (
        f"chore(release): {plan.library} {plan.proposed_version}\n\n"
        f"Includes: {keys}\nRelease-Plan: {plan.plan_id}\n"
    )


def verify_release_branch(
    git: Git, plan: ReleasePlan, ref: str | None = None
) -> VerificationReport:
    ref = ref or plan.branch
    head = git.try_rev_parse(ref)
    report = VerificationReport(ok=True, branch=ref, head=head)
    if head is None:
        report.add("branch_exists", False, f"'{ref}' does not exist")
        return report
    report.add("branch_exists", True, head)
    if not git.is_ancestor(plan.base_revision, head):
        report.add("based_on_planned_base", False, f"{plan.base_revision[:10]} is not an ancestor")
        return report
    report.add("based_on_planned_base", True, plan.base_revision)

    commits = [
        git.commit_info(sha)
        for sha in git.rev_list(f"{plan.base_revision}..{head}", no_merges=False)
    ]
    expected = plan.included_commits
    expect_version_commit = plan.version_file is not None
    expected_count = len(expected) + (1 if expect_version_commit else 0)
    report.add(
        "commit_count",
        len(commits) == expected_count,
        f"expected {expected_count}, found {len(commits)}",
    )
    if any(len(c.parents) > 1 for c in commits):
        report.add("no_merge_commits", False, "release branch contains merge commits")

    for planned, actual in zip(expected, commits, strict=False):
        source = _PICKED_RE.findall(actual.body)
        ok = bool(source) and source[-1] == planned.sha
        report.add(
            f"provenance:{planned.sha[:10]}",
            ok,
            f"{actual.sha[:10]} cherry-picked from {source[-1][:10] if source else 'nothing'}",
        )
        patch_ok = git.patch_id(actual.sha) == planned.release_patch_id
        report.add(
            f"content:{planned.sha[:10]}",
            patch_ok,
            "patch-id matches" if patch_ok else "patch-id differs from the planned commit",
        )
        report.provenance.append({"source": planned.sha, "result": actual.sha})

    if expect_version_commit and len(commits) == expected_count:
        last = commits[-1]
        files = [f.path for f in last.files]
        ok_files = files == [plan.version_file]
        content = git.show_file(last.sha, str(plan.version_file)) or ""
        ok_version = (
            f'"{plan.proposed_version}"' in content or f"'{plan.proposed_version}'" in content
        )
        report.add(
            "version_commit",
            ok_files and ok_version,
            f"touches {files}; sets version {plan.proposed_version}: {ok_version}",
        )
    return report
