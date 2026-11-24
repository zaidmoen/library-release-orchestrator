"""Cross-repository impact matrix (ADR-0017).

Builds on what the planner already computes per configured consumer (``ConsumerTarget``:
the declared pin, the library modules it imports, the action), and adds:

* the library modules the release changes, and the **subset each consumer uses**;
* confirmed source incompatibilities: a removed public symbol that the consumer imports;
* README-discovered consumers that couldn't be analysed: **UNRESOLVED**, never safe;
* the validation status recorded by ``update-consumers`` (passed / failed / not run).

Decisions (evidence-backed, never certainty the evidence doesn't give):

=========================  ==============================================================
UPGRADE_RECOMMENDED        it imports a changed module, or the ticket targets it
BREAKING_IMPACT            it imports a symbol the release removes (code change needed)
NOT_REQUIRED_STATIC        no import of a changed module (static only; runtime not verified)
ALREADY_COMPATIBLE         its pin is already at or above the proposed version
MANUAL                     not an ``==`` pin, or an unsupported manifest format
UNRESOLVED                 imports not inspected, or the repository isn't readable
NOT_A_CONSUMER             no declared dependency
=========================  ==============================================================
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from release_orchestrator.analysis.python_symbols import module_name
from release_orchestrator.consumers.readme_discovery import (
    CONFIRMED,
    NOT_A_CONSUMER,
    DiscoveryResult,
)
from release_orchestrator.gitutil.repo import Git
from release_orchestrator.planning.plan import ReleasePlan

_DECISION = {
    "update": "UPGRADE_RECOMMENDED",
    "up_to_date": "ALREADY_COMPATIBLE",
    "manual": "MANUAL",
    "not_targeted": "NOT_REQUIRED_STATIC",
    "not_a_consumer": "NOT_A_CONSUMER",
}
_ACTION = {
    "UPGRADE_RECOMMENDED": "bump the pin on a confirmed branch and open a Draft PR",
    "BREAKING_IMPACT": "code change required before upgrading (removed API in use)",
    "NOT_REQUIRED_STATIC": "none required by static evidence; run its tests if in doubt",
    "ALREADY_COMPATIBLE": "none",
    "MANUAL": "update the constraint by hand (not an exact '==' pin)",
    "UNRESOLVED": "make it readable (clone or GitHub MCP) before deciding",
    "NOT_A_CONSUMER": "none",
}


@dataclass
class ImpactRow:
    service: str
    repository: str
    current_version: str | None
    proposed_version: str
    affected_modules: list[str]
    decision: str
    required_action: str
    validation_status: str
    evidence: list[str] = field(default_factory=list)
    confidence: str = "static"  # static | declared-only | none

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def changed_modules(plan: ReleasePlan, git: Git) -> set[str]:
    """Library modules touched by the commits that *ship* in this plan."""
    prefix = plan.package_dir.strip("/") + "/"
    modules: set[str] = set()
    for change in plan.included:
        for commit in change.commits:
            for f in git.commit_info(commit.sha).files:
                for path in (f.path, f.old_path):
                    if path and path.startswith(prefix):
                        name = module_name(path, plan.package_dir)
                        if name:
                            modules.add(name)
    return modules


def _validation(run_steps: dict[str, Any], service: str) -> str:
    updated = run_steps.get("update-consumers", {}).get("result", {}).get("updated", [])
    for entry in updated:
        if entry.get("consumer") == service:
            status = (entry.get("validation") or {}).get("status")
            return {"PASSED": "tests passed", "FAILED": "tests failed"}.get(
                str(status), f"prepared ({entry.get('status')})"
            )
    return "not run"


def impact_matrix(
    plan: ReleasePlan,
    git: Git,
    *,
    repositories: dict[str, str],
    discovery: DiscoveryResult | None = None,
    run_steps: dict[str, Any] | None = None,
) -> dict[str, Any]:
    changed = changed_modules(plan, git)
    removed = set(plan.analysis.get("public_api_removals", []))
    top = plan.package_dir.strip("/").rsplit("/", 1)[-1]
    rows: list[ImpactRow] = []
    covered: set[str] = set()
    for c in plan.consumers:
        imported = set(c.get("imported_library_modules", c.get("imported_modules", [])))
        used = sorted(
            m
            for m in changed
            if any(i == m or i.startswith(m + ".") or m.startswith(i + ".") for i in imported)
            or top in imported
        )
        decision = _DECISION.get(c["action"], "UNRESOLVED")
        evidence = list(c.get("reasons", []))
        breaking = sorted(s for s in removed if s in imported or s.rsplit(".", 1)[0] in imported)
        if breaking and c["action"] != "not_a_consumer":
            decision = "BREAKING_IMPACT"
            evidence.append(f"imports removed public API: {', '.join(breaking)}")
        if used:
            evidence.append(f"uses changed modules: {', '.join(used)}")
        elif decision == "NOT_REQUIRED_STATIC":
            evidence.append("no import of a changed module found (runtime not verified)")
        rows.append(
            ImpactRow(
                service=c["name"],
                repository=repositories.get(c["name"], c["name"]),
                current_version=c.get("current_version"),
                proposed_version=plan.proposed_version,
                affected_modules=used,
                decision=decision,
                required_action=_ACTION[decision],
                validation_status=_validation(run_steps or {}, c["name"]),
                evidence=evidence,
                confidence="static" if c["action"] != "not_a_consumer" else "none",
            )
        )
        covered.add(c["name"])
    for ev in discovery.consumers if discovery else []:
        name = ev.local_path.replace("\\", "/").rsplit("/", 1)[-1] if ev.local_path else ev.name
        if name in covered or ev.status == NOT_A_CONSUMER:
            continue
        declared = ev.status == CONFIRMED
        rows.append(
            ImpactRow(
                service=ev.name,
                repository=ev.repository or ev.local_path or ev.name,
                current_version=ev.declaration.version if ev.declaration else None,
                proposed_version=plan.proposed_version,
                affected_modules=[],
                decision="UNRESOLVED",
                required_action=_ACTION["UNRESOLVED"],
                validation_status="not run",
                evidence=[*ev.evidence, f"discovery status: {ev.status}"],
                confidence="declared-only" if declared else "none",
            )
        )
    recommended = [r.service for r in rows if r.decision in ("UPGRADE_RECOMMENDED",)]
    unresolved = [r.service for r in rows if r.decision in ("UNRESOLVED", "BREAKING_IMPACT")]
    return {
        "library": plan.library,
        "proposed_version": plan.proposed_version,
        "changed_modules": sorted(changed),
        "rows": [r.to_dict() for r in rows],
        "upgrade_recommended": recommended,
        "needs_attention": unresolved,
        "complete": discovery is None or discovery.complete,
        "note": "Static analysis isn't proof of runtime compatibility; only consumer tests "
        "against the published artifact are.",
    }
