"""The release plan / release manifest (schema_version 1).

The plan is pure data: deterministic, free of timestamps and machine-specific paths, so
that identical inputs produce byte-identical JSON and the same ``plan_id``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = 1
PLAN_STATUSES = ("PLAN_READY", "BLOCKED")
WRITE_STEPS = ("prepare", "pr", "publish", "update-consumers")


@dataclass(frozen=True)
class PlannedCommit:
    sha: str
    subject: str
    author: str
    patch_id: str  # of the source commit
    release_patch_id: str  # expected once applied onto the base (from the sandbox probe)

    def to_dict(self) -> dict[str, str]:
        return {"sha": self.sha, "subject": self.subject, "author": self.author,
                "patch_id": self.patch_id, "release_patch_id": self.release_patch_id}


@dataclass(frozen=True)
class PlannedChange:
    key: str
    title: str
    commits: tuple[PlannedCommit, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "title": self.title,
                "commits": [c.to_dict() for c in self.commits]}


@dataclass(frozen=True)
class ExcludedChange:
    key: str
    title: str
    commits: tuple[str, ...]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "title": self.title, "commits": list(self.commits),
                "reason": self.reason}


@dataclass
class ReleasePlan:
    library: str
    package_dir: str
    version_file: str | None
    request: dict[str, Any]
    base_ref: str
    base_revision: str
    source_ref: str
    source_revision: str
    branch: str
    apply_order: list[str]
    version: dict[str, Any]
    included: list[PlannedChange]
    excluded: list[ExcludedChange]
    already_released: list[str]
    prerequisites: list[str]
    analysis: dict[str, Any]
    consumers: list[dict[str, Any]]
    compatibility_risks: list[str]
    validation_required: list[dict[str, Any]]
    remediation: list[dict[str, Any]]
    blockers: list[dict[str, Any]]
    status: str = "PLAN_READY"
    plan_id: str = field(default="")

    def __post_init__(self) -> None:
        if not self.plan_id:
            self.plan_id = compute_plan_id(self.identity())

    @property
    def proposed_version(self) -> str:
        return str(self.version["proposed_version"])

    @property
    def included_shas(self) -> list[str]:
        return list(self.apply_order)

    @property
    def included_commits(self) -> list[PlannedCommit]:
        """Included commits in original topological (application) order."""
        by_sha = {c.sha: c for change in self.included for c in change.commits}
        return [by_sha[sha] for sha in self.apply_order]

    @property
    def included_keys(self) -> list[str]:
        return [c.key for c in self.included]

    def identity(self) -> dict[str, Any]:
        """The content that defines *what* is released. Hashing it yields ``plan_id``."""
        return {
            "library": self.library,
            "base_revision": self.base_revision,
            "commits": self.included_shas,
            "release_patch_ids": [c.release_patch_id for c in self.included_commits],
            "version": self.proposed_version,
            "version_file": self.version_file,
            "consumers": sorted((c["name"], c["action"]) for c in self.consumers),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "plan_id": self.plan_id,
            "status": self.status,
            "library": {"name": self.library, "package_dir": self.package_dir,
                        "version_file": self.version_file},
            "request": self.request,
            "release": {
                "base_ref": self.base_ref,
                "base_revision": self.base_revision,
                "source_ref": self.source_ref,
                "source_revision": self.source_revision,
                "branch": self.branch,
                "apply_order": self.apply_order,
                **self.version,
            },
            "included_changes": [c.to_dict() for c in self.included],
            "excluded_changes": [c.to_dict() for c in self.excluded],
            "already_released": self.already_released,
            "prerequisites": self.prerequisites,
            "analysis": self.analysis,
            "consumers": self.consumers,
            "compatibility_risks": self.compatibility_risks,
            "validation": {"required": self.validation_required, "status": "pending",
                           "evidence": "see run state; a manifest never proves its own validation"},
            "approval": {"required": True, "required_for": list(WRITE_STEPS)},
            "remediation": self.remediation,
            "blockers": self.blockers,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=False) + "\n"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReleasePlan:
        release = dict(data["release"])
        version = {k: release.pop(k) for k in
                   ("previous_version", "proposed_version", "bump", "reasons", "collisions",
                    "status") if k in release}
        lib = data["library"]
        return cls(
            library=lib["name"],
            package_dir=lib["package_dir"],
            version_file=lib.get("version_file"),
            request=data["request"],
            base_ref=release["base_ref"],
            base_revision=release["base_revision"],
            source_ref=release["source_ref"],
            source_revision=release["source_revision"],
            branch=release["branch"],
            apply_order=list(release.get("apply_order", [])),
            version=version,
            included=[
                PlannedChange(c["key"], c.get("title", ""), tuple(
                    PlannedCommit(m["sha"], m.get("subject", ""), m.get("author", ""),
                                  m.get("patch_id", ""), m.get("release_patch_id", ""))
                    for m in c["commits"]))
                for c in data["included_changes"]
            ],
            excluded=[
                ExcludedChange(c["key"], c.get("title", ""), tuple(c.get("commits", [])),
                               c.get("reason", ""))
                for c in data["excluded_changes"]
            ],
            already_released=list(data.get("already_released", [])),
            prerequisites=list(data.get("prerequisites", [])),
            analysis=data.get("analysis", {}),
            consumers=list(data.get("consumers", [])),
            compatibility_risks=list(data.get("compatibility_risks", [])),
            validation_required=list(data.get("validation", {}).get("required", [])),
            remediation=list(data.get("remediation", [])),
            blockers=list(data.get("blockers", [])),
            status=data["status"],
            plan_id=data["plan_id"],
        )


def compute_plan_id(identity: dict[str, Any]) -> str:
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
