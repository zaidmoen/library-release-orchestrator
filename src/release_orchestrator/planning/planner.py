"""Release planning engine: turns a resolved request into a deterministic ``ReleasePlan``.

Planning never writes to a repository. Execution (``isolation``) is a separate step that
consumes an approved plan.
"""

from __future__ import annotations

from typing import Any

from release_orchestrator.analysis.dependency import AnalysisResult, DependencyAnalyzer, all_units
from release_orchestrator.analysis.python_symbols import module_name
from release_orchestrator.config import Config
from release_orchestrator.consumers.discovery import assess_consumer
from release_orchestrator.errors import BlockedError, ConfigError, NotFoundError
from release_orchestrator.gitutil.history import HistoryScan
from release_orchestrator.gitutil.repo import Git
from release_orchestrator.integrations.jira import IssueTracker
from release_orchestrator.models import Severity, Verdict
from release_orchestrator.planning.manifest import expected_validation
from release_orchestrator.planning.plan import (
    ExcludedChange,
    PlannedChange,
    PlannedCommit,
    ReleasePlan,
)
from release_orchestrator.requests import ResolvedRequest
from release_orchestrator.versioning.policy import recommend_version
from release_orchestrator.versioning.registry import PackageRegistry
from release_orchestrator.versioning.semver import SemVer


def resolve_base(config: Config, git: Git, override: str | None) -> str:
    """Explicit --base, then config ``base_ref``, then the highest release tag."""
    if override:
        return override
    if config.library.base_ref:
        return config.library.base_ref
    prefix = config.library.tag_prefix
    tagged = [(v, t) for t in git.tags(f"{prefix}*")
              if (v := SemVer.try_parse(t[len(prefix):])) and not v.is_prerelease]
    if not tagged:
        raise ConfigError(
            "No release tag found to use as base; pass --base or set library.base_ref",
            details={"tag_pattern": f"{prefix}*"},
        )
    return max(tagged)[1]


def tagged_versions(config: Config, git: Git) -> list[SemVer]:
    prefix = config.library.tag_prefix
    return [v for t in git.tags(f"{prefix}*") if (v := SemVer.try_parse(t[len(prefix):]))]


def build_plan(
    *,
    config: Config,
    git: Git,
    scan: HistoryScan,
    request: ResolvedRequest,
    tracker: IssueTracker,
    registry: PackageRegistry,
) -> tuple[ReleasePlan, AnalysisResult]:
    requested = list(request.change_keys)
    pending = [k for k in requested if k in scan.changes]
    released = [k for k in requested if k not in scan.changes and k in scan.already_released]
    missing = [k for k in requested if k not in scan.changes and k not in scan.already_released]
    if missing:
        raise NotFoundError(
            f"No pending commits found for {', '.join(missing)} between "
            f"{scan.base_ref} and {scan.source_ref}",
            details={"keys": missing, "base": scan.base_sha, "source": scan.source_sha},
        )
    if not pending:
        raise BlockedError(
            f"Nothing to release: {', '.join(released)} already present in {scan.base_ref}",
            reason="NOTHING_TO_RELEASE",
        )

    analyzer = DependencyAnalyzer(git, config.library.package_dir)
    analysis = analyzer.analyze(scan, pending)
    units = all_units(scan)

    order = scan.commit_order()
    included_shas = [sha for sha in order if any(sha in units[k].shas for k in pending)]
    included_keys = sorted(pending, key=lambda k: order.index(units[k].commits[0].sha))
    probe_ids = analysis.probe.result_patch_ids if analysis.probe else {}
    included = [
        PlannedChange(
            key=k,
            title=_title(tracker, k, units[k].title),
            commits=tuple(
                PlannedCommit(c.sha, c.subject, f"{c.author_name} <{c.author_email}>",
                              scan.patch_ids.get(c.sha, ""), probe_ids.get(c.sha, ""))
                for c in units[k].commits
            ),
        )
        for k in included_keys
    ]
    excluded = [
        ExcludedChange(
            key=k, title=_title(tracker, k, unit.title), commits=unit.shas,
            reason=_exclusion_reason(k, analysis, request),
        )
        for k, unit in units.items() if k not in pending
    ]

    all_commits = [c for k in pending for c in units[k].commits]
    decision = recommend_version(
        all_commits,
        published=registry.list_versions(config.library.name),
        tagged=tagged_versions(config, git),
        api_removals=analysis.api_removals,
    )

    affected = _affected_modules(units, pending, config.library.package_dir)
    requested_consumers: set[str] = set()
    for key in pending:
        issue = tracker.get(key)
        if issue:
            requested_consumers.update(issue.consumers)
    known = {c.name for c in config.consumers}
    unknown_consumers = sorted(requested_consumers - known)
    top_package = config.library.package_dir.strip("/").rsplit("/", 1)[-1]
    consumers = [
        assess_consumer(
            c, library=config.library.name, top_package=top_package,
            affected_modules=affected, requested_by_ticket=requested_consumers & known,
            proposed=decision.proposed,
        ).to_dict()
        for c in config.consumers
    ]

    blockers: list[dict[str, Any]] = [
        {"reason": str(f.kind), "message": f.message, "change": f.change,
         "depends_on": f.depends_on}
        for f in analysis.blockers
    ]
    if decision.collides:
        blockers.append({"reason": "VERSION_COLLISION", "message": "; ".join(decision.collisions)})
    if not config.library_validation:
        blockers.append({"reason": "NO_VALIDATION_CONFIGURED",
                         "message": "No library validation commands are configured"})

    risks = [f.message for f in analysis.findings if f.severity is Severity.WARNING]
    if unknown_consumers:
        risks.append(f"Ticket names unknown consumers (ignored): {unknown_consumers}")
    if decision.bump.value == "major":
        risks.append("Major version: consumers must review breaking changes before upgrading")
    risks += [f"{c['name']}: {'; '.join(c['reasons'])}" for c in consumers if c["action"] == "manual"]

    plan = ReleasePlan(
        library=config.library.name,
        package_dir=config.library.package_dir,
        version_file=config.library.version_file,
        request=request.to_dict(),
        base_ref=scan.base_ref,
        base_revision=scan.base_sha,
        source_ref=scan.source_ref,
        source_revision=scan.source_sha,
        branch=f"{config.library.release_branch_prefix}{decision.proposed}",
        apply_order=included_shas,
        version=decision.to_dict(),
        included=included,
        excluded=excluded,
        already_released=sorted(set(released) | set(scan.already_released)),
        prerequisites=analysis.prerequisites,
        analysis=analysis.to_dict(),
        consumers=consumers,
        compatibility_risks=risks,
        validation_required=expected_validation(
            config, [c["name"] for c in consumers if c["action"] == "update"]),
        remediation=_remediation(analysis, pending, decision.collides),
        blockers=blockers,
        status="BLOCKED" if blockers else "PLAN_READY",
    )
    return plan, analysis


def _title(tracker: IssueTracker, key: str, fallback: str) -> str:
    issue = tracker.get(key)
    return issue.summary if issue and issue.summary else fallback


def _affected_modules(units: dict[str, Any], keys: list[str], package_dir: str) -> set[str]:
    prefix = package_dir.strip("/") + "/"
    modules = set()
    for key in keys:
        for path in units[key].files:
            if path.startswith(prefix):
                name = module_name(path, package_dir)
                if name:
                    modules.add(name)
    return modules


def _exclusion_reason(key: str, analysis: AnalysisResult, request: ResolvedRequest) -> str:
    if key in analysis.prerequisites:
        return "required by a requested change but not requested (see blockers)"
    if key in request.deferred_keys:
        return "deferred by request"
    if key.startswith("UNMAPPED:"):
        return "commit not linked to any change key"
    if key.startswith("AMBIGUOUS:"):
        return "commit linked to several change keys"
    return "not requested"


def _remediation(analysis: AnalysisResult, pending: list[str], collision: bool) -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    if analysis.verdict is Verdict.DEPENDENT:
        together = sorted(set(pending) | set(analysis.prerequisites))
        options += [
            {"option": "include_prerequisites", "changes": together,
             "description": "Release the requested change together with its prerequisites. "
                            "Re-run plan with these keys."},
            {"option": "extract_shared_part",
             "description": "Move only the code the requested change needs out of the "
                            "prerequisite into its own small change, release it first, then "
                            "rebase the requested change on it."},
            {"option": "feature_flag_prerequisite",
             "description": "Ship the prerequisite disabled behind a feature flag in the same "
                            "release, then enable it when the business approves."},
            {"option": "release_in_development_order",
             "description": "Release the prerequisite first, as developed."},
        ]
    if analysis.verdict is Verdict.CONFLICT:
        causes = sorted({str(c) for f in analysis.findings if f.kind.value == "GIT_CONFLICT"
                         for c in f.evidence.get("suspected_causes", [])})
        options += [
            {"option": "backport_commit",
             "description": "Have the change author create a backport commit of the requested "
                            "change on the release base, resolving the conflict manually and "
                            "keeping the same change key. Never auto-resolve business logic."},
            {"option": "include_conflicting_changes", "changes": sorted(set(pending) | set(causes)),
             "description": "Release together with the change(s) that touch the same lines."},
        ]
    if any(f.kind.value == "AMBIGUOUS_COMMIT" for f in analysis.blockers):
        options.append({"option": "clarify_commit_mapping",
                        "description": "Split the multi-ticket commit or confirm its owner, "
                                       "then re-plan."})
    if collision:
        options.append({"option": "investigate_version_collision",
                        "description": "A tag or artifact already uses the proposed version. "
                                       "Check for a concurrent or half-finished release, then "
                                       "re-plan; versions are never overwritten."})
    return options
