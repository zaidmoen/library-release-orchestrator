"""Environment promotion ledger (ADR-0007).

Each environment (``prep``, ``stage``, ``master`` ...) has an append-only ledger of
promotion records pinning an immutable, published artifact by version and digest.
Nothing here deploys anything: a ledger record is the hand-off to a deploy system.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from release_orchestrator.config import Config, EnvironmentConfig
from release_orchestrator.errors import ApprovalRequiredError, BlockedError, InvalidInputError
from release_orchestrator.versioning.registry import PackageRegistry
from release_orchestrator.versioning.semver import SemVer


@dataclass(frozen=True)
class PromotionRecord:
    environment: str
    version: str
    sha256: str
    approved_by: tuple[str, ...]
    previous_version: str | None
    kind: str  # promotion | rollback
    at: str
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "environment": self.environment,
            "version": self.version,
            "sha256": self.sha256,
            "approved_by": list(self.approved_by),
            "previous_version": self.previous_version,
            "kind": self.kind,
            "at": self.at,
            "note": self.note,
        }


class EnvironmentLedger:
    def __init__(self, config: Config, registry: PackageRegistry) -> None:
        if not config.environments:
            raise InvalidInputError("No environments are configured")
        self.config = config
        self.registry = registry
        self.dir = config.state_dir / "environments"

    # ------------------------------------------------------------------ reads
    def env(self, name: str) -> EnvironmentConfig:
        for env in self.config.environments:
            if env.name == name:
                return env
        raise InvalidInputError(
            f"Unknown environment '{name}'",
            details={"environments": [e.name for e in self.config.environments]},
        )

    def history(self, name: str) -> list[dict[str, Any]]:
        self.env(name)
        path = self.dir / f"{name}.json"
        if not path.is_file():
            return []
        data: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
        return data

    def current(self, name: str) -> str | None:
        records = self.history(name)
        return str(records[-1]["version"]) if records else None

    def status(self) -> dict[str, Any]:
        return {
            e.name: {
                "current": self.current(e.name),
                "required_approvals": e.required_approvals,
                "allow_prerelease": e.allow_prerelease,
                "records": len(self.history(e.name)),
            }
            for e in self.config.environments
        }

    # ------------------------------------------------------------------ promotion
    def promote(
        self, version: str, env_name: str, *, approved_by: list[str], approve: bool
    ) -> dict[str, Any]:
        env = self.env(env_name)
        semver = SemVer.parse(version)
        digest = self._verified_digest(semver)
        if self.current(env.name) == str(semver):
            return {"status": "already_current", "environment": env.name, "version": str(semver)}
        if semver.is_prerelease and not env.allow_prerelease:
            raise BlockedError(
                f"{env.name} does not accept pre-release versions ({semver})",
                reason="PRERELEASE_NOT_ALLOWED",
            )
        previous_env = self._previous(env)
        if previous_env is not None and not any(
            r["version"] == str(semver) for r in self.history(previous_env.name)
        ):
            raise BlockedError(
                f"{semver} has not been promoted to {previous_env.name} yet; "
                f"environments must be promoted in order",
                reason="PROMOTION_ORDER",
                details={"required_first": previous_env.name},
            )
        self._check_approvals(env, approved_by, approve, f"promote {semver} to {env.name}")
        record = self._append(env.name, str(semver), digest, approved_by, "promotion", "")
        return {"status": "promoted", "record": record.to_dict()}

    def rollback_plan(self, env_name: str) -> dict[str, Any]:
        env = self.env(env_name)
        records = self.history(env.name)
        if not records:
            raise BlockedError(
                f"{env.name} has no promotion history", reason="NOTHING_TO_ROLL_BACK"
            )
        current = records[-1]["version"]
        target = next((r for r in reversed(records) if r["version"] != current), None)
        if target is None:
            raise BlockedError(
                f"{env.name} has never run a version other than {current}",
                reason="NOTHING_TO_ROLL_BACK",
            )
        semver = SemVer.parse(target["version"])
        artifact = self.registry.get(self.config.library.name, semver)
        digest_ok = artifact is not None and artifact.sha256 == target["sha256"]
        if digest_ok:
            self.registry.fetch(self.config.library.name, semver)  # raises if tampered
        return {
            "environment": env.name,
            "from_version": current,
            "to_version": target["version"],
            "to_sha256": target["sha256"],
            "artifact_verified": digest_ok,
            "required_approvals": env.required_approvals,
            "method": "roll forward to the previous immutable artifact; no tag, branch or "
            "published version is modified",
            "consumer_actions": [
                f"Consumers pinned to {current} that must also revert: re-pin "
                f"{self.config.library.name}=={target['version']} (review per service)"
            ],
        }

    def rollback(self, env_name: str, *, approved_by: list[str], approve: bool) -> dict[str, Any]:
        plan = self.rollback_plan(env_name)
        if not plan["artifact_verified"]:
            raise BlockedError(
                "Rollback target artifact is missing or its digest changed",
                reason="ROLLBACK_TARGET_UNVERIFIED",
                details=plan,
            )
        env = self.env(env_name)
        self._check_approvals(env, approved_by, approve, f"roll back {env.name}")
        record = self._append(
            env.name,
            plan["to_version"],
            plan["to_sha256"],
            approved_by,
            "rollback",
            f"rollback from {plan['from_version']}",
        )
        return {"status": "rolled_back", "plan": plan, "record": record.to_dict()}

    # ------------------------------------------------------------------ helpers
    def _previous(self, env: EnvironmentConfig) -> EnvironmentConfig | None:
        names = [e.name for e in self.config.environments]
        index = names.index(env.name)
        return self.config.environments[index - 1] if index else None

    def _verified_digest(self, version: SemVer) -> str:
        artifact = self.registry.get(self.config.library.name, version)
        if artifact is None:
            raise BlockedError(
                f"{self.config.library.name} {version} is not published; only immutable "
                "artifacts can be promoted",
                reason="VERSION_NOT_PUBLISHED",
            )
        self.registry.fetch(self.config.library.name, version)  # verifies the digest
        return artifact.sha256

    @staticmethod
    def _check_approvals(
        env: EnvironmentConfig, approved_by: list[str], approve: bool, action: str
    ) -> None:
        approvers = sorted({a.strip() for a in approved_by if a.strip()})
        if not approve:
            raise ApprovalRequiredError(f"'{action}' changes release state and needs --approve")
        if len(approvers) < env.required_approvals:
            raise BlockedError(
                f"{env.name} requires {env.required_approvals} distinct approver(s); "
                f"got {len(approvers)}",
                reason="INSUFFICIENT_APPROVALS",
                details={"approved_by": approvers},
            )

    def _append(
        self,
        env: str,
        version: str,
        digest: str,
        approved_by: list[str],
        kind: str,
        note: str,
    ) -> PromotionRecord:
        records = self.history(env)
        record = PromotionRecord(
            environment=env,
            version=version,
            sha256=digest,
            approved_by=tuple(sorted({a.strip() for a in approved_by if a.strip()})),
            previous_version=records[-1]["version"] if records else None,
            kind=kind,
            at=datetime.now(UTC).isoformat(timespec="seconds"),
            note=note,
        )
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / f"{env}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps([*records, record.to_dict()], indent=2), encoding="utf-8")
        os.replace(tmp, path)  # append-only: we only ever rewrite with the old records kept
        return record


def ledger_path(config: Config, env: str) -> Path:
    return config.state_dir / "environments" / f"{env}.json"
