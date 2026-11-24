"""Release operation modes: ``release-candidate``, ``release`` and ``reorder-releases`` (ADR-0014).

Every mode follows the same deterministic, read-only workflow that the Claude Skill
drives interactively:

* **Discovery (step 0)**: stable release, pending release candidates and their Git
  ancestry, pending trunk changes, consumer services and their pins, environments and
  their configured branches, GitHub refs and PR records, issue-tracker snapshot, approval
  policy. Anything that can't be read is reported as an *uncertainty*, never filled in.
* **Q1, dependency decision (single choice)**: options are generated from evidence
  (`build_plan`, `DependencyAnalyzer`, issue links, Git ancestry). Each option is
  *valid* or *blocked* with the reasons. At most one is *recommended*.
* **Q2, services (multi select)** and **Q3, environments (optional multi select)**
  only scope the *operation*. They never narrow the analysis.
* **Preview**: per-service and per-environment status (READY / BLOCKED / NEEDS
  VALIDATION / NOT SELECTED), the exact operations and commands, and a fingerprint of
  the discovered state. ``check_preview`` refuses a preview whose fingerprint no longer
  matches (stale plan).

Nothing here writes to a repository, a registry or the run state. A preview is never
approval: execution happens through the existing ``--approve``-gated commands.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from typing import Any

from release_orchestrator.analysis.python_symbols import module_name
from release_orchestrator.consumers.discovery import find_requirement, is_impacted, library_imports
from release_orchestrator.errors import (
    BlockedError,
    GitError,
    IntegrationError,
    InvalidInputError,
    NotFoundError,
    OrchestratorError,
)
from release_orchestrator.gitutil.history import HistoryScan, scan_history
from release_orchestrator.gitutil.repo import Git, optimizations_enabled
from release_orchestrator.integrations.jira import NullIssueTracker
from release_orchestrator.integrations.pull_requests import ExternalPullRequestProvider
from release_orchestrator.models import BumpLevel
from release_orchestrator.planning.plan import ReleasePlan
from release_orchestrator.planning.planner import build_plan
from release_orchestrator.requests import ResolvedRequest
from release_orchestrator.versioning.semver import SemVer

MODES = ("release-candidate", "release", "reorder-releases")
READY, BLOCKED, NEEDS_VALIDATION, NOT_SELECTED = (
    "READY",
    "BLOCKED",
    "NEEDS VALIDATION",
    "NOT SELECTED",
)
STOP = "stop"


@dataclass
class Candidate:
    version: str
    tag: str
    sha: str
    keys: list[str]
    contains: list[str]  # other pending candidates whose tag is an ancestor of this one
    own_keys: list[str]  # keys not already in a contained candidate
    published: bool | None  # None = registry unreadable

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Step:
    """One library release in an option: what ships, at which version, with what verdict."""

    label: str
    keys: list[str]
    version: str
    source: str
    base: str
    prerelease: str | None
    status: str  # PLAN_READY | BLOCKED | NO_OP
    plan_id: str | None = None
    candidate: str | None = None
    blockers: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Reorder (ADR-0017): the *new* immutable candidate tag allocated for this content.
    # Existing candidate tags are never moved, deleted or re-used.
    new_candidate: str | None = None


@dataclass
class Option:
    id: str
    label: str
    description: str
    valid: bool
    recommended: bool = False
    evidence: list[str] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        produces = [
            {
                "step": n,
                "candidate": s.candidate,
                "tickets": s.keys,
                "version": s.version,
                "status": s.status,
                "new_candidate": s.new_candidate,
            }
            for n, s in enumerate(self.steps, 1)
        ]
        return {**asdict(self), "steps": [asdict(s) for s in self.steps], "produces": produces}


def _releases_summary(disc: dict[str, Any]) -> dict[str, Any]:
    """The releases Q1 is about: latest stable, pending RCs (with contents), finalized RCs."""
    return {
        "latest_stable": disc["latest_stable"],
        "pending_candidates": [
            {
                "tag": c["tag"],
                "version": c["version"],
                "sha": c["sha"],
                "tickets": c["keys"],
                "own_tickets": c["own_keys"],
                "contains": c["contains"],
                "published": c["published"],
            }
            for c in disc["pending_candidates"]
        ],
        "finalized_candidates": disc["finalized_candidates"],
    }


def _releases_text(releases: dict[str, Any]) -> str:
    stable = releases["latest_stable"]
    parts = [f"Latest stable: {stable['tag']}" if stable else "No stable release"]
    pending = [
        f"{c['tag']} ({', '.join(c['tickets']) or 'no tickets'}"
        + (f"; contains {', '.join(c['contains'])}" if c["contains"] else "")
        + ")"
        for c in releases["pending_candidates"]
    ]
    parts.append(f"Pending RCs: {'; '.join(pending)}" if pending else "Pending RCs: none")
    if releases["finalized_candidates"]:
        parts.append(f"Finalized RCs: {', '.join(releases['finalized_candidates'])}")
    return ". ".join(parts) + "."


class ModeEngine:
    def __init__(self, orch: Any) -> None:  # Orchestrator (typed loosely to avoid a cycle)
        self.orch = orch
        self.config = orch.config
        self.git: Git = orch.s.git
        self._plans: dict[tuple[Any, ...], tuple[ReleasePlan | None, dict[str, Any] | None]] = {}
        self._discovery: dict[str, Any] | None = None
        self._scans: dict[tuple[str, str, str, str], HistoryScan] = {}

    # ------------------------------------------------------------------ discovery
    def discover(self) -> dict[str, Any]:
        if self._discovery is not None:
            return self._discovery
        cfg, lib = self.config, self.config.library
        uncertainties: list[str] = []
        prefix = lib.tag_prefix
        versions: dict[SemVer, str] = {}
        for tag in self.git.tags(f"{prefix}*"):
            parsed = SemVer.try_parse(tag[len(prefix) :])
            if parsed is not None:
                versions[parsed] = tag
        finals = sorted(v for v in versions if not v.is_prerelease)
        stable = finals[-1] if finals else None
        published = self._published_versions(uncertainties)
        final_cores = {(v.major, v.minor, v.patch) for v in finals} | {
            (v.major, v.minor, v.patch) for v in published or [] if not v.is_prerelease
        }
        source_sha = self.git.rev_parse(lib.source_ref)

        candidates: list[Candidate] = []
        superseded: list[str] = []
        for v in sorted(versions):
            if not v.is_prerelease:
                continue
            tag = versions[v]
            if (v.major, v.minor, v.patch) in final_cores:
                superseded.append(tag)  # already finalized
                continue
            if stable is not None and v <= stable:
                continue  # an RC of an older line, overtaken by a later stable release
            sha = self.git.rev_parse(tag)
            keys: list[str] = []
            if stable is not None:
                scan = self._scan(versions[stable], tag)
                keys = sorted(scan.changes)
            candidates.append(Candidate(str(v), tag, sha, keys, [], keys, self._is_published(v)))
        for cand in candidates:
            for other in candidates:
                if (
                    other is not cand
                    and other.sha != cand.sha
                    and self.git.is_ancestor(other.sha, cand.sha)
                ):
                    cand.contains.append(other.tag)
            contained = {k for o in candidates if o.tag in cand.contains for k in o.keys}
            cand.own_keys = [k for k in cand.keys if k not in contained]
        if stable is None:
            uncertainties.append("no stable release tag found; RC contents can't be computed")

        pending: list[dict[str, Any]] = []
        if stable is not None:
            scan = self._scan(versions[stable], lib.source_ref)
            for key, unit in scan.changes.items():
                issue = self.orch.s.tracker.get(key)
                pending.append(
                    {
                        "key": key,
                        "title": unit.title,
                        "commits": len(unit.commits),
                        "issue_status": issue.status if issue else None,
                        "blocked_by": list(issue.blocked_by) if issue else [],
                        "in_candidates": [c.tag for c in candidates if key in c.keys],
                    }
                )
            if scan.unmapped:
                uncertainties.append(
                    f"{len(scan.unmapped)} commit(s) on {lib.source_ref} map to no "
                    "ticket; they are excluded and analysed as such"
                )

        services = [self._service(c, uncertainties) for c in cfg.consumers]
        environments = self._environments(uncertainties)
        github = self._github(stable, versions, uncertainties)
        tracker = self.orch.s.tracker
        tracker_info: dict[str, Any] = (
            {"configured": False}
            if isinstance(tracker, NullIssueTracker)
            else {
                "configured": True,
                "issues": len(tracker.keys()),
                "fetched_at": (tracker.fetched_at() or None) and str(tracker.fetched_at()),
            }
        )
        if isinstance(tracker, NullIssueTracker):
            uncertainties.append("no issue tracker configured; ticket status and links unverified")
        policy = cfg.approval_policy
        discovery = {
            "library": lib.name,
            "latest_stable": (
                {
                    "version": str(stable),
                    "tag": versions[stable],
                    "sha": self.git.rev_parse(versions[stable]),
                }
                if stable
                else None
            ),
            "pending_candidates": [c.to_dict() for c in candidates],
            "finalized_candidates": superseded,
            "source": {"ref": lib.source_ref, "sha": source_sha},
            "pending_changes": pending,
            "services": services,
            "environments": environments,
            "github": github,
            "issue_tracker": tracker_info,
            "approval_policy": asdict(policy) if policy else None,
            "registry": {
                "provider": cfg.registry_provider,
                "production": cfg.registry_production,
                "published_versions": [str(v) for v in published or []],
            },
            "uncertainties": uncertainties,
        }
        discovery["fingerprint"] = _fingerprint(
            {
                "tags": {t: self.git.rev_parse(t) for t in sorted(versions.values())},
                "source": source_sha,
                "published": discovery["registry"]["published_versions"],
                "services": [(s["name"], s["current_version"]) for s in services],
                "environments": [(e["name"], e["current"]) for e in environments],
            }
        )
        self._discovery = discovery
        return discovery

    def candidates(self) -> list[Candidate]:
        return [Candidate(**c) for c in self.discover()["pending_candidates"]]

    # ------------------------------------------------------------------ Q1
    def options(
        self,
        mode: str,
        *,
        tickets: Iterable[str] = (),
        candidate: str | None = None,
        order: Iterable[str] = (),
    ) -> dict[str, Any]:
        if mode not in MODES:
            raise InvalidInputError(f"Unknown mode {mode!r}", details={"modes": MODES})
        disc = self.discover()
        if disc["latest_stable"] is None:
            raise BlockedError("No stable release exists to plan from", reason="NO_STABLE_RELEASE")
        if mode == "release-candidate":
            options = self._rc_options(list(tickets))
        elif mode == "release":
            options = self._release_options(candidate)
        else:
            options = self._reorder_options(list(order))
        options.append(Option(STOP, "Stop", "Do nothing; resolve prerequisites first.", True))
        valid = [o for o in options if o.valid and o.id != STOP]
        recommended = next((o for o in valid if o.recommended), None)
        releases = _releases_summary(disc)
        return {
            "mode": mode,
            "question": "Dependency check: how should pending changes be handled?",
            "releases": releases,
            "releases_text": _releases_text(releases),
            "single_choice": True,
            "options": [o.to_dict() for o in options],
            "recommended": recommended.id if recommended else STOP,
            "fingerprint": disc["fingerprint"],
        }

    def _rc_options(self, tickets: list[str]) -> list[Option]:
        disc = self.discover()
        pending = [p["key"] for p in disc["pending_changes"]]
        if not pending:
            return [
                Option(
                    "no-op",
                    "Nothing to release",
                    "No pending changes on the source branch since the stable release.",
                    False,
                    evidence=["pending_changes is empty"],
                )
            ]
        unknown = [t for t in tickets if t not in pending]
        if unknown:
            raise NotFoundError(f"No pending commits for {unknown}", details={"pending": pending})
        requested = tickets or pending
        stable_tag = disc["latest_stable"]["tag"]
        source = self.config.library.source_ref
        step = self._step("requested", requested, stable_tag, source, "rc")
        options: list[Option] = []
        if step.status == "PLAN_READY":
            options.append(
                Option(
                    "requested",
                    f"Release candidate with {', '.join(requested)}",
                    "The requested changes are independent of everything left out.",
                    True,
                    True,
                    evidence=[f"plan {step.plan_id}: no blockers", *step.warnings],
                    steps=[step],
                )
            )
            return options
        plan, _ = self._plan(requested, stable_tag, source, "rc")
        prereqs = [p for p in (plan.prerequisites if plan else []) if p in pending]
        options.append(
            Option(
                "requested-only",
                f"Only {', '.join(requested)}",
                "Release exactly what was asked, without its prerequisites.",
                False,
                evidence=[b["message"] for b in step.blockers],
                steps=[step],
            )
        )
        if prereqs:
            closure = sorted(set(requested) | set(prereqs))
            with_pre = self._step("with-prerequisites", closure, stable_tag, source, "rc")
            ok = with_pre.status == "PLAN_READY"
            options.append(
                Option(
                    "with-prerequisites",
                    f"Include prerequisites: {', '.join(closure)}",
                    f"Ship {', '.join(prereqs)} with the request, as code analysis and "
                    "issue links require.",
                    ok,
                    ok,
                    evidence=[
                        f"{b['change']} -> {b.get('depends_on')}: {b['reason']}"
                        for b in step.blockers
                        if b.get("depends_on")
                    ]
                    + [b["message"] for b in with_pre.blockers],
                    steps=[with_pre],
                )
            )
        return options

    def _release_options(self, requested: str | None) -> list[Option]:
        disc = self.discover()
        cands = self.candidates()
        if requested is not None and requested not in {c.tag for c in cands} | {
            c.version for c in cands
        }:
            raise NotFoundError(
                f"{requested} is not a pending release candidate",
                details={"pending": [c.tag for c in cands]},
            )
        if not cands:
            return [
                Option(
                    "no-op",
                    "No pending release candidates",
                    "Every RC is already finalized, or none exists.",
                    False,
                    evidence=[f"finalized: {disc['finalized_candidates']}"],
                )
            ]
        stable_tag = disc["latest_stable"]["tag"]
        options: list[Option] = []
        for cand in sorted(cands, key=lambda c: SemVer.parse(c.version), reverse=True):
            rc = SemVer.parse(cand.version)
            final = SemVer(rc.major, rc.minor, rc.patch)
            step = self._step(
                f"release {cand.tag}", cand.keys, stable_tag, cand.tag, None, candidate=cand.tag
            )
            evidence = [
                f"{cand.tag} contains {', '.join(cand.keys)} (Git range {stable_tag}..{cand.tag})"
            ]
            if step.status == "PLAN_READY" and step.version != str(final):
                step.status = "BLOCKED"
                step.blockers.append(
                    {
                        "reason": "VERSION_PROGRESSION",
                        "message": f"{cand.tag} targets {final}, but its content would release as "
                        f"{step.version} given the published/tagged versions",
                    }
                )
            if cand.published is False:
                step.warnings.append(
                    f"{cand.tag} was never published as an artifact; its final "
                    "release is validated from source only"
                )
            if cand.contains:
                evidence.append(
                    f"supersedes {', '.join(cand.contains)} (their commits are ancestors)"
                )
            ok = step.status == "PLAN_READY"
            options.append(
                Option(
                    f"release:{cand.tag}",
                    f"Release {cand.tag} as {final}",
                    "Finalize this candidate. The final release must equal the candidate's content "
                    "except the version file (checked with verify-lineage).",
                    ok,
                    evidence=evidence + [b["message"] for b in step.blockers] + step.warnings,
                    steps=[step],
                )
            )
        chosen = next(
            (o for o in options if o.valid and requested and o.id.endswith(requested)), None
        ) or next((o for o in options if o.valid), None)
        if chosen:
            chosen.recommended = True
        return options

    def _reorder_options(self, order: list[str]) -> list[Option]:
        cands = {c.tag: c for c in self.candidates()}
        by_version = {c.version: c.tag for c in cands.values()}
        order = [by_version.get(o, o) for o in order]
        unknown = [o for o in order if o not in cands]
        finalized = self.discover()["finalized_candidates"]
        done = [
            o
            for o in unknown
            if o in finalized or f"{self.config.library.tag_prefix}{o}" in finalized
        ]
        if done:
            raise BlockedError(
                f"{done} already have a final release, so they can't be reordered; "
                f"latest stable is {self.discover()['latest_stable']['tag']}",
                reason="ALREADY_FINALIZED",
                details={"finalized": finalized, "pending": sorted(cands)},
            )
        if unknown:
            raise NotFoundError(
                f"Not pending candidates: {unknown}", details={"pending": sorted(cands)}
            )
        if len(cands) < 2:
            return [
                Option(
                    "no-op", "Nothing to reorder", f"Only {len(cands)} pending candidate(s).", False
                )
            ]
        source = self.config.library.source_ref
        if not all(self.git.is_ancestor(c.sha, self.git.rev_parse(source)) for c in cands.values()):
            return [
                Option(
                    "unsupported",
                    "Ordering can't be analysed",
                    f"Some candidates are not on {source}; ordering analysis needs them "
                    "on the source branch.",
                    False,
                )
            ]
        ancestry = sorted(
            cands, key=lambda t: (len(cands[t].contains), SemVer.parse(cands[t].version))
        )
        options: list[Option] = []
        relations = [
            f"{t} contains {', '.join(cands[t].contains)} (Git ancestry)"
            for t in ancestry
            if cands[t].contains
        ]
        if order and order != ancestry:
            options.append(self._order_option("requested", order, cands, relations))
        options.append(
            self._order_option("dependency-order", ancestry, cands, relations, recommended=True)
        )
        top = ancestry[-1]
        if set(cands[top].contains) == set(cands) - {top}:
            sup = self._order_option("supersede", [top], cands, relations, whole=True)
            sup.label = f"Release only {top} (supersedes {', '.join(cands[top].contains)})"
            sup.description = (
                "Ship the newest candidate, which already contains the others; "
                "the older candidates are retired, not released."
            )
            options.append(sup)
        if options[0].id == "requested" and options[0].valid:
            options[0].recommended, options[1].recommended = True, False
        return options

    def _order_option(
        self,
        oid: str,
        order: list[str],
        cands: dict[str, Candidate],
        relations: list[str],
        recommended: bool = False,
        whole: bool = False,
    ) -> Option:
        disc = self.discover()
        base = disc["latest_stable"]["tag"]
        source = self.config.library.source_ref
        released: set[str] = set()
        steps: list[Step] = []
        version = SemVer.parse(disc["latest_stable"]["version"])
        for tag in order:
            cand = cands[tag]
            if steps and steps[-1].status == "BLOCKED":
                steps.append(
                    Step(
                        f"{tag} (skipped)",
                        [],
                        "-",
                        tag,
                        base,
                        None,
                        "SKIPPED",
                        candidate=tag,
                        warnings=["not evaluated: an earlier step is blocked"],
                    )
                )
                continue
            pool = cand.keys if whole else cand.own_keys
            new = [k for k in pool if k not in released] or [
                k for k in cand.keys if k not in released
            ]
            if not new:
                steps.append(
                    Step(
                        f"{tag} (no-op)",
                        [],
                        str(version),
                        tag,
                        base,
                        None,
                        "NO_OP",
                        candidate=tag,
                        warnings=["everything in it is already released"],
                    )
                )
                continue
            step = self._step(
                f"{tag}: {', '.join(new)}",
                sorted(released | set(new)),
                base,
                source,
                None,
                candidate=tag,
            )
            # The step ships only its new keys; earlier steps' keys are already in the base.
            step.keys = sorted(new)
            if step.status == "PLAN_READY":
                rc = SemVer.parse(cand.version)
                stable = SemVer.parse(disc["latest_stable"]["version"])
                version = version.bump(_level_between(stable, SemVer.parse(step.version)))
                step.version = str(version)
                if (version.major, version.minor, version.patch) != (rc.major, rc.minor, rc.patch):
                    step.warnings.append(
                        f"{tag} targets {rc.major}.{rc.minor}.{rc.patch} but would "
                        f"release as {version}; re-cut the candidate"
                    )
                if not released:  # the next release: a new candidate, never a renamed one
                    rc_plan, _ = self._plan(sorted(new), base, source, "rc")
                    if rc_plan is not None:
                        step.new_candidate = (
                            f"{self.config.library.tag_prefix}{rc_plan.proposed_version}"
                        )
                        if step.new_candidate != tag:
                            step.warnings.append(
                                f"a new candidate {step.new_candidate} would be cut from {base} "
                                f"with {', '.join(sorted(new))}; {tag} stays as it is"
                            )
            released |= set(new)
            steps.append(step)
        ok = all(s.status not in ("BLOCKED", "SKIPPED") for s in steps)
        evidence = list(relations)
        for s in steps:
            evidence += [f"{s.label}: {b['message']}" for b in s.blockers] + s.warnings
        return Option(
            oid,
            " -> ".join(order),
            "Release each candidate's own changes in this "
            "order; earlier steps are in the base of later ones.",
            ok,
            recommended and ok,
            evidence=evidence,
            steps=steps,
        )

    # ------------------------------------------------------------------ preview
    def preview(
        self,
        mode: str,
        *,
        choice: str,
        services: Iterable[str] | None,
        environments: Iterable[str] | None,
        tickets: Iterable[str] = (),
        candidate: str | None = None,
        order: Iterable[str] = (),
    ) -> dict[str, Any]:
        disc = self.discover()
        q1 = self.options(mode, tickets=tickets, candidate=candidate, order=order)
        chosen = next((o for o in q1["options"] if o["id"] == choice), None)
        if chosen is None:
            raise InvalidInputError(
                f"Unknown choice {choice!r}", details={"choices": [o["id"] for o in q1["options"]]}
            )
        known_services = [s["name"] for s in disc["services"]]
        selected = _selection(services, known_services, "service")
        env_names = [e["name"] for e in disc["environments"]]
        selected_envs = _selection(environments, env_names, "environment", default_all=False)

        steps = chosen["steps"]
        blockers: list[str] = []
        warnings: list[str] = list(disc["uncertainties"])
        if choice == STOP:
            blockers.append("Stop selected: no operation will be performed")
        elif not chosen["valid"]:
            # Only real blockers block. Context such as Git ancestry ("rc.23 contains
            # rc.22") or overlap warnings stays in warnings (ADR-0015).
            step_blockers = [
                f"{s['label']}: {b['message']}" for s in steps for b in s.get("blockers", [])
            ]
            blockers += step_blockers or chosen["evidence"] or [f"option {choice} is blocked"]
            warnings += [e for e in chosen["evidence"] if e not in blockers and e not in warnings]
        live_steps = [s for s in steps if s["status"] not in ("NO_OP", "SKIPPED")]
        stable = disc["latest_stable"]
        target = live_steps[-1]["version"] if live_steps else stable["version"]
        keys = sorted({k for s in live_steps for k in s["keys"]})

        library_status = BLOCKED if blockers else NEEDS_VALIDATION
        if not blockers and live_steps and all(self._validated(s["plan_id"]) for s in live_steps):
            library_status = READY
        policy = disc["approval_policy"]
        approval = self._approval_status(live_steps, policy)
        if library_status == READY and approval["required"] and not approval["verified"]:
            library_status = NEEDS_VALIDATION
        rows = [
            {
                "service": disc["library"],
                "current_version": stable["version"],
                "target_version": target,
                "environments": selected_envs or ["none"],
                "status": library_status,
                "selected": True,
                "affected": True,
                "notes": "library release" + ("" if live_steps else " (no-op)"),
            }
        ]
        affected_modules = self._affected_modules(keys)
        top = self.config.library.package_dir.strip("/").rsplit("/", 1)[-1]
        for svc in disc["services"]:
            imported = set(svc["imported_library_modules"])
            affected = (
                bool(keys)
                and svc["declares_library"]
                and is_impacted(imported, affected_modules, top)
            )
            row = {
                "service": svc["name"],
                "current_version": svc["current_version"],
                "target_version": target if affected else svc["current_version"],
                "environments": selected_envs or ["none"],
                "selected": svc["name"] in selected,
                "affected": affected,
            }
            if not row["selected"]:
                row["status"] = NOT_SELECTED
                row["notes"] = (
                    "affected but not selected; its pin stays unchanged"
                    if affected
                    else "not affected"
                )
                if affected:
                    warnings.append(f"{svc['name']} is affected by this release but not selected")
            elif not svc["accessible"] or not svc["declares_library"]:
                row["status"] = BLOCKED if not svc["accessible"] else READY
                row["notes"] = svc["note"]
                if not svc["accessible"]:
                    blockers.append(f"{svc['name']}: {svc['note']}")
            elif not affected:
                row["status"], row["notes"] = READY, "not affected; no change needed"
            elif library_status == BLOCKED:
                row["status"], row["notes"] = BLOCKED, "blocked by the library release"
            elif svc["operator"] != "==":
                row["status"], row["notes"] = NEEDS_VALIDATION, "pin isn't '=='; update manually"
            else:
                row["status"] = NEEDS_VALIDATION
                row["notes"] = "consumer tests run against the published artifact at update time"
            rows.append(row)

        env_rows = self._env_rows(selected_envs, target, live_steps, blockers)
        operations, commands = self._operations(mode, live_steps, rows, env_rows, stable["tag"])
        if not selected_envs:
            warnings.append(
                "No environment selected: the safe default is NO environment "
                "operation (prep/stage/master untouched)."
            )
        for env in env_rows:
            if env["status"] == BLOCKED:
                blockers.append(f"environment {env['environment']}: {env['notes']}")
        for row in rows:
            if row["selected"] and row["status"] == BLOCKED:
                message = f"{row['service']}: {row.get('notes', 'blocked')}"
                if message not in blockers:
                    blockers.append(message)
        statuses = [r["status"] for r in rows if r["selected"]] + [e["status"] for e in env_rows]
        readiness = (
            BLOCKED
            if blockers or BLOCKED in statuses
            else NEEDS_VALIDATION
            if NEEDS_VALIDATION in statuses
            else READY
        )
        preview = {
            "mode": mode,
            "choice": chosen,
            "latest_stable": stable,
            "pending_candidates": disc["pending_candidates"],
            "proposed_version": target,
            "execution_order": {
                "existing": [
                    c["tag"]
                    for c in sorted(
                        disc["pending_candidates"], key=lambda c: SemVer.parse(c["version"])
                    )
                ],
                "proposed": [s["candidate"] or s["label"] for s in steps],
            },
            "dependencies": self._dependencies(steps, keys),
            "selected_services": selected,
            "affected_services": [r["service"] for r in rows[1:] if r["affected"]],
            "services": rows,
            "environments": env_rows,
            "environment_mapping": [
                {
                    "environment": e["name"],
                    "branch": e["branch"] or "not configured",
                    "current_version": e["current"],
                    "selected": e["name"] in selected_envs,
                    "allow_prerelease": e["allow_prerelease"],
                    "required_approvals": e["required_approvals"],
                }
                for e in disc["environments"]
            ],
            "release_steps": [
                {
                    "step": n,
                    "candidate": s["candidate"],
                    "tickets": s["keys"],
                    "version": s["version"],
                    "status": s["status"],
                    "plan_id": s["plan_id"],
                    "new_candidate": s.get("new_candidate"),
                }
                for n, s in enumerate(steps, 1)
            ],
            "operations": operations,
            "commands": commands,
            "approval": approval,
            "blockers": blockers,
            "warnings": warnings,
            "readiness": readiness,
            "fingerprint": disc["fingerprint"],
            "executed": False,
            "note": "A preview is not approval. Each command marked --approve needs an explicit "
            "human yes at execution time.",
        }
        preview["preview_id"] = _fingerprint(
            {k: preview[k] for k in ("mode", "choice", "selected_services", "fingerprint")}
            | {"envs": selected_envs}
        )
        return preview

    def _dependencies(self, steps: list[dict[str, Any]], keys: list[str]) -> dict[str, Any]:
        """Relationships with their evidence source. Nothing here is inferred by Claude.

        Git ancestry ("rc.23 contains rc.22") is reported under ``ancestry``: it describes
        content, and is never presented as a functional dependency (ADR-0015).
        """
        disc = self.discover()
        ancestry = [
            {"from": c["tag"], "to": other, "evidence": "git ancestry (contains its commits)"}
            for c in disc["pending_candidates"]
            for other in c["contains"]
        ]
        confirmed: list[dict[str, Any]] = []
        relevant = {k for step in steps for k in step["keys"]} | {
            b.get("change") for step in steps for b in step["blockers"]
        }
        for change in (c for c in disc["pending_changes"] if c["key"] in relevant):
            for blocker in change["blocked_by"]:
                confirmed.append(
                    {
                        "from": change["key"],
                        "to": blocker,
                        "evidence": "issue tracker 'blocked by'",
                        "satisfied": blocker in keys or change["key"] not in keys,
                    }
                )
        unresolved = [
            {
                "from": b.get("change"),
                "to": b.get("depends_on"),
                "reason": b["reason"],
                "evidence": b["message"],
            }
            for step in steps
            for b in step["blockers"]
        ]
        return {"ancestry": ancestry, "confirmed": confirmed, "unresolved": unresolved}

    def check_preview(self, preview: dict[str, Any]) -> dict[str, Any]:
        current = self.discover()["fingerprint"]
        if preview.get("fingerprint") != current:
            raise BlockedError(
                "The repository, registry or environment state changed since this preview; "
                "regenerate it before executing anything",
                reason="STALE_PREVIEW",
                details={"preview": preview.get("fingerprint"), "current": current},
            )
        return {"fresh": True, "fingerprint": current, "preview_id": preview.get("preview_id")}

    # ------------------------------------------------------------------ helpers
    def _scan(self, base: str, source: str) -> HistoryScan:
        if not optimizations_enabled():
            return scan_history(
                self.git,
                base_ref=base,
                source_ref=source,
                key_pattern=self.config.change_key_pattern,
            )
        # Refs are resolved on every call (mutable); the scan of a SHA range is immutable.
        key = (self.git.rev_parse(base), self.git.rev_parse(source), base, source)
        if key not in self._scans:
            self._scans[key] = scan_history(
                self.git,
                base_ref=base,
                source_ref=source,
                key_pattern=self.config.change_key_pattern,
            )
        return self._scans[key]

    def _plan(
        self, keys: list[str], base: str, source: str, prerelease: str | None
    ) -> tuple[ReleasePlan | None, dict[str, Any] | None]:
        cache_key = (tuple(sorted(keys)), base, source, prerelease)
        if cache_key not in self._plans:
            try:
                scan = self._scan(base, source)
                request = ResolvedRequest("ticket", " ".join(keys), tuple(sorted(keys)))
                plan, _ = build_plan(
                    config=self.config,
                    git=self.git,
                    scan=scan,
                    request=request,
                    tracker=self.orch.s.tracker,
                    registry=self.orch.s.registry,
                    prerelease=prerelease,
                )
                self._plans[cache_key] = (plan, None)
            except OrchestratorError as exc:
                self._plans[cache_key] = (None, exc.to_dict())
        return self._plans[cache_key]

    def _step(
        self,
        label: str,
        keys: list[str],
        base: str,
        source: str,
        prerelease: str | None,
        candidate: str | None = None,
    ) -> Step:
        plan, error = self._plan(keys, base, source, prerelease)
        if plan is None:
            error = error or {"code": "UNKNOWN", "message": "plan failed"}
            return Step(
                label,
                sorted(keys),
                "-",
                source,
                base,
                prerelease,
                "BLOCKED",
                candidate=candidate,
                blockers=[
                    {"reason": error.get("reason", error["code"]), "message": error["message"]}
                ],
            )
        return Step(
            label,
            sorted(keys),
            plan.proposed_version,
            source,
            base,
            prerelease,
            plan.status,
            plan.plan_id,
            candidate,
            list(plan.blockers),
            list(plan.compatibility_risks),
        )

    def _affected_modules(self, keys: list[str]) -> set[str]:
        disc = self.discover()
        if not keys or disc["latest_stable"] is None:
            return set()
        scan = self._scan(disc["latest_stable"]["tag"], self.config.library.source_ref)
        package_dir = self.config.library.package_dir.strip("/")
        modules = set()
        for key in keys:
            unit = scan.changes.get(key)
            for path in unit.files if unit else ():
                if path.startswith(package_dir + "/"):
                    name = module_name(path, package_dir)
                    if name:
                        modules.add(name)
        return modules

    def _validated(self, plan_id: str | None) -> bool:
        if not plan_id:
            return False
        run = self.orch.s.runs.load(plan_id)
        result = run.steps.get("validate", {}).get("result", {}) if run else {}
        return result.get("status") == "PASSED"

    def _approval_status(
        self, steps: list[dict[str, Any]], policy: dict[str, Any] | None
    ) -> dict[str, Any]:
        if policy is None:
            return {
                "required": False,
                "verified": False,
                "detail": "no approval_policy configured; publication needs --approve only"
                + (
                    " and is refused for production registries"
                    if self.config.registry_production
                    else ""
                ),
            }
        prs = self.orch.s.prs
        recorded = []
        if isinstance(prs, ExternalPullRequestProvider):
            for step in steps:
                if step["plan_id"]:
                    plan_branch = f"{self.config.library.release_branch_prefix}{step['version']}"
                    record = prs.get(self.config.library.name, plan_branch)
                    if record and record.get("number"):
                        recorded.append(record["number"])
        return {
            "required": True,
            "verified": False,
            "policy": policy,
            "recorded_prs": recorded,
            "detail": "verified only by `pr-status` against GitHub after the PR exists; "
            "not yet verified"
            if not recorded
            else "PR recorded; run `pr-status` to verify approvals and checks",
        }

    def _env_rows(
        self, selected: list[str], target: str, steps: list[dict[str, Any]], blockers: list[str]
    ) -> list[dict[str, Any]]:
        disc = self.discover()
        rows = []
        is_pre = SemVer.parse(target).is_prerelease if target and target != "-" else False
        names = [e["name"] for e in disc["environments"]]
        for env in disc["environments"]:
            if env["name"] not in selected:
                continue
            row = {
                "environment": env["name"],
                "branch": env["branch"] or "not configured",
                "current_version": env["current"],
                "target_version": target,
                "required_approvals": env["required_approvals"],
            }
            index = names.index(env["name"])
            previous = names[index - 1] if index else None
            if blockers or not steps:
                row["status"], row["notes"] = BLOCKED, "no releasable version"
            elif is_pre and not env["allow_prerelease"]:
                row["status"], row["notes"] = BLOCKED, f"{env['name']} doesn't accept pre-releases"
            elif (
                previous
                and previous not in selected
                and disc["environments"][index - 1]["current"] != target
            ):
                row["status"] = BLOCKED
                row["notes"] = f"promotion order: {target} must reach {previous} first"
            elif env["branch"] is None:
                row["status"] = NEEDS_VALIDATION
                row["notes"] = "branch mapping not configured; promotion is ledger-only"
            else:
                row["status"] = NEEDS_VALIDATION
                row["notes"] = (
                    f"needs a published artifact and {env['required_approvals']} approver(s)"
                )
            rows.append(row)
        return rows

    def _operations(
        self,
        mode: str,
        steps: list[dict[str, Any]],
        rows: list[dict[str, Any]],
        env_rows: list[dict[str, Any]],
        stable_tag: str,
    ) -> tuple[list[str], list[str]]:
        operations: list[str] = []
        commands: list[str] = []
        lib = self.config.library
        consumers = [r["service"] for r in rows[1:] if r["selected"] and r["affected"]]
        base = stable_tag
        for n, step in enumerate(steps, 1):
            version = step["version"]
            plan_file = f"plan-{n}.json"
            branch = f"{lib.release_branch_prefix}{version}"
            operations += [
                f"create branch {branch} from {base} with {', '.join(step['keys'])} (+ version "
                "commit)",
                f"push {branch} (+ release-base/{base}) to the allowlisted remote",
                f"draft PR {branch} -> release-base/{base}",
                f"publish {lib.name} {version} to the {self.config.registry_provider} registry "
                f"and tag {lib.tag_prefix}{version}",
            ]
            tickets = " ".join(f"--ticket {k}" for k in step["keys"])
            pre = " --prerelease rc" if step["prerelease"] else ""
            source = step["source"]
            commands += [
                f"release-orchestrator plan {tickets} --base {base} --source {source}{pre} "
                f"--out {plan_file}",
                f"release-orchestrator prepare --plan {plan_file} --approve",
                f"release-orchestrator validate --plan {plan_file}",
            ]
            if mode == "release" and step["candidate"]:
                commands.append(
                    f"release-orchestrator verify-lineage --plan {plan_file} "
                    f"--candidate {step['candidate']}"
                )
            commands += [
                f"release-orchestrator pr --plan {plan_file} --approve",
                f"release-orchestrator push --plan {plan_file} --approve",
                f"release-orchestrator reconcile-prs --plan {plan_file}",
            ]
            if self.config.approval_policy is not None:
                commands.append(f"release-orchestrator pr-status --plan {plan_file}")
            commands.append(f"release-orchestrator publish --plan {plan_file} --approve")
            if consumers and n == len(steps):
                selected = " ".join(f"--consumer {c}" for c in consumers)
                commands.append(
                    f"release-orchestrator update-consumers --plan {plan_file} {selected} --approve"
                )
                operations.append(
                    f"dependency-update branches + draft PRs for {', '.join(consumers)}"
                )
            commands.append(f"release-orchestrator push --plan {plan_file} --approve")
            base = f"{lib.tag_prefix}{version}"
        for env in env_rows:
            if env["status"] == BLOCKED:
                continue
            operations.append(
                f"record promotion of {env['target_version']} to {env['environment']}"
                f" (ledger; branch {env['branch']})"
            )
            commands.append(
                f"release-orchestrator promote --version {env['target_version']} "
                f"--env {env['environment']} --approved-by <name> --approve"
            )
        return operations, commands

    def _published_versions(self, uncertainties: list[str]) -> list[SemVer] | None:
        try:
            return list(self.orch.s.registry.list_versions(self.config.library.name))
        except (IntegrationError, OSError) as exc:
            uncertainties.append(f"registry unreadable: {exc}")
            return None

    def _is_published(self, version: SemVer) -> bool | None:
        try:
            return self.orch.s.registry.get(self.config.library.name, version) is not None
        except (IntegrationError, OSError):
            return None

    def _service(self, consumer: Any, uncertainties: list[str]) -> dict[str, Any]:
        info: dict[str, Any] = {
            "name": consumer.name,
            "repo": str(consumer.repo),
            "current_version": None,
            "operator": None,
            "declares_library": False,
            "imported_library_modules": [],
            "accessible": True,
            "note": "",
        }
        git = Git(consumer.repo)
        try:
            if not git.is_repo():
                raise GitError("not a Git repository")
            rev = git.rev_parse(consumer.source_ref)
            text = git.show_file(rev, consumer.dependency_file)
            req = find_requirement(text, self.config.library.name) if text is not None else None
            top = self.config.library.package_dir.strip("/").rsplit("/", 1)[-1]
            info["imported_library_modules"] = sorted(library_imports(git, rev, top))
            if req is None:
                info["note"] = (
                    f"{consumer.dependency_file} doesn't declare {self.config.library.name}"
                )
            else:
                info.update(
                    current_version=req.version or None,
                    operator=req.operator,
                    declares_library=True,
                )
        except (GitError, OSError) as exc:
            info.update(accessible=False, note=f"repository inaccessible: {exc}")
            uncertainties.append(f"{consumer.name}: repository inaccessible ({consumer.repo})")
        return info

    def _environments(self, uncertainties: list[str]) -> list[dict[str, Any]]:
        from release_orchestrator.environments import EnvironmentLedger

        if not self.config.environments:
            uncertainties.append("no environments configured")
            return []
        ledger = EnvironmentLedger(self.config, self.orch.s.registry)
        envs = []
        for env in self.config.environments:
            if env.branch is None:
                uncertainties.append(f"environment {env.name}: no branch mapping configured")
            envs.append(
                {
                    "name": env.name,
                    "branch": env.branch,
                    "current": ledger.current(env.name),
                    "allow_prerelease": env.allow_prerelease,
                    "required_approvals": env.required_approvals,
                }
            )
        return envs

    def _github(
        self, stable: SemVer | None, versions: dict[SemVer, str], uncertainties: list[str]
    ) -> dict[str, Any]:
        remote = self.config.remote_name
        try:
            url = self.git.remote_url(remote)
        except GitError:
            uncertainties.append(f"library has no '{remote}' remote; GitHub state not checked")
            return {"remote": None, "verified": False}
        try:
            remote_tags = {
                name[len("refs/tags/") :]: sha
                for sha, name in self.git.ls_remote(remote, "refs/tags/*")
                if not name.endswith("^{}")
            }
            remote_prs = len(self.git.ls_remote(remote, "refs/pull/*/head"))
        except (GitError, IntegrationError) as exc:
            uncertainties.append(f"GitHub remote unreadable: {exc}")
            return {"remote": url, "verified": False}
        missing = [t for t in versions.values() if t not in remote_tags]
        diverged = [
            t
            for t in versions.values()
            if t in remote_tags and remote_tags[t] != self.git.rev_parse(t)
        ]
        if diverged:
            uncertainties.append(f"tags differ between local and remote: {diverged}")
        prefix = self.config.library.tag_prefix
        remote_only = sorted(
            t
            for t in remote_tags
            if t.startswith(prefix)
            and SemVer.try_parse(t[len(prefix) :]) is not None
            and t not in versions.values()
        )
        if remote_only:
            uncertainties.append(
                f"release tags exist on GitHub but not locally: {remote_only}; run "
                f"`git fetch {remote} --tags` (read-only for the remote) and rediscover"
            )
        return {
            "remote": url,
            "verified": True,
            "tags_missing_on_remote": missing,
            "tags_missing_locally": remote_only,
            "tags_diverged": diverged,
            "pull_request_refs": remote_prs,
            "releases": self._github_releases(url, versions, uncertainties),
        }

    def _github_releases(
        self, url: str, versions: dict[SemVer, str], uncertainties: list[str]
    ) -> dict[str, Any]:
        """GitHub Release objects (metadata). Git tags stay the source of release content."""
        from release_orchestrator.context import parse_remote

        parsed = parse_remote(url)
        if parsed is None or parsed[0] != "github.com":
            return {"read": False, "note": "remote is not on github.com"}
        reader = getattr(self.orch.s, "github", None)
        if reader is None:
            return {"read": False, "note": "no GitHub reader"}
        try:
            releases = reader.releases(parsed[1])
        except OrchestratorError as exc:
            uncertainties.append(f"GitHub releases unreadable for {parsed[1]}: {exc.message}")
            return {"read": False, "repository": parsed[1], "note": exc.message}
        tags = set(versions.values())
        prefix = self.config.library.tag_prefix
        mismatched = []
        for rel in releases:
            parsed_version = SemVer.try_parse(rel.tag[len(prefix) :])
            if parsed_version is not None and parsed_version.is_prerelease != rel.prerelease:
                mismatched.append(
                    f"{rel.tag}: GitHub prerelease={rel.prerelease} but the version "
                    f"{'is' if parsed_version.is_prerelease else 'is not'} a pre-release"
                )
        if mismatched:
            uncertainties.append(
                "GitHub release flags disagree with versions: " + "; ".join(mismatched)
            )
        return {
            "read": True,
            "repository": parsed[1],
            "releases": [r.to_dict() for r in releases],
            "without_tag": sorted(r.tag for r in releases if r.tag not in tags and not r.draft),
            "tags_without_release": sorted(tags - {r.tag for r in releases}),
            "flag_mismatches": mismatched,
        }


# --------------------------------------------------------------------------- utilities
def _selection(
    values: Iterable[str] | None, known: list[str], what: str, default_all: bool = True
) -> list[str]:
    """Validate a Q2/Q3 selection. ``all`` selects everything; omission = default."""
    if values is None:
        return list(known) if default_all else []
    chosen = [v.strip() for v in values if v.strip()]
    if chosen == ["all"]:
        return list(known)
    unknown = [v for v in chosen if v not in known]
    if unknown:
        raise InvalidInputError(f"Unknown {what}(s): {unknown}", details={"known": known})
    return [k for k in known if k in chosen]  # stable order


def _level_between(stable: SemVer, planned: SemVer) -> BumpLevel:
    """The bump level the planner chose relative to the stable release."""
    if planned.major > stable.major:
        return BumpLevel.MAJOR
    if planned.minor > stable.minor:
        return BumpLevel.MINOR
    return BumpLevel.PATCH


def _fingerprint(data: Any) -> str:
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def render_preview(preview: dict[str, Any]) -> str:
    """Compact human-readable impact preview (the JSON is the source of truth)."""
    stable = preview["latest_stable"]
    lines = [
        f"# Impact preview: {preview['mode']} ({preview['readiness']})",
        "",
        "Nothing has been executed. Each command marked `--approve` needs an explicit yes.",
        "",
        f"* Choice: **{preview['choice']['label']}** (`{preview['choice']['id']}`)",
        f"* Latest stable release: **{stable['version']}** "
        f"(`{stable['tag']}`, {stable['sha'][:10]})",
        f"* Proposed version: **{preview['proposed_version']}**",
        f"* Affected microservices: {', '.join(preview['affected_services']) or 'none'}",
        f"* Selected microservices: {', '.join(preview['selected_services']) or 'none'}",
        "",
        "## Pending release candidates",
        "",
    ]
    if preview["pending_candidates"]:
        lines += [
            "| Candidate | Tickets | Contains | Own tickets | Published |",
            "|---|---|---|---|---|",
        ]
        lines += [
            f"| {c['tag']} | {', '.join(c['keys'])} | {', '.join(c['contains']) or '-'} | "
            f"{', '.join(c['own_keys'])} | {c['published']} |"
            for c in preview["pending_candidates"]
        ]
    else:
        lines.append("None pending.")
    order = preview["execution_order"]
    lines += [
        "",
        "## Execution order",
        "",
        f"Existing: {' -> '.join(order['existing']) or 'none'}",
        "",
        "| Step | Candidate | Tickets | Version | Status |",
        "|---|---|---|---|---|",
    ]
    lines += [
        f"| {s['step']} | {s['candidate'] or '-'} | {', '.join(s['tickets']) or '-'} | "
        f"{s['version']} | {s['status']} |"
        for s in preview["release_steps"]
    ]
    lines += [
        "",
        "## Services",
        "",
        "| Service | Current Version | Target Version | Environment | Status | Notes |",
        "|---|---|---|---|---|---|",
    ]
    for row in preview["services"]:
        lines.append(
            f"| {row['service']} | {row['current_version'] or '-'} | "
            f"{row['target_version'] or '-'} | {', '.join(row['environments'])} | "
            f"{row['status']} | {row.get('notes', '')} |"
        )
    lines += [
        "",
        "## Environments and branches",
        "",
        "| Environment | Branch | Current | Selected | Target | Status | Notes |",
        "|---|---|---|---|---|---|---|",
    ]
    selected = {e["environment"]: e for e in preview["environments"]}
    for env in preview["environment_mapping"]:
        row = selected.get(env["environment"])
        lines.append(
            f"| {env['environment']} | {env['branch']} | {env['current_version'] or '-'} | "
            f"{'yes' if row else 'no'} | {row['target_version'] if row else '-'} | "
            f"{row['status'] if row else NOT_SELECTED} | "
            f"{row['notes'] if row else 'no operation (not selected)'} |"
        )
    deps = preview["dependencies"]
    lines += ["", "## Dependencies", ""]
    lines += [
        f"* ancestry (content, not a functional dependency): {d['from']} contains {d['to']}"
        for d in deps.get("ancestry", [])
    ]
    lines += [f"* confirmed: {d['from']} -> {d['to']} ({d['evidence']})" for d in deps["confirmed"]]
    lines += [
        f"* **unresolved**: {d['from']} -> {d['to']}: {d['evidence']}" for d in deps["unresolved"]
    ] or ["* no unresolved dependencies"]
    lines += ["", "## Approval / CI", "", f"* {preview['approval']['detail']}"]
    if preview["blockers"]:
        lines += ["", "## Blockers", ""] + [f"* {b}" for b in preview["blockers"]]
    if preview["warnings"]:
        lines += ["", "## Warnings", ""] + [f"* {w}" for w in preview["warnings"]]
    lines += ["", "## Operations (not executed)", ""] + [f"1. {o}" for o in preview["operations"]]
    lines += [
        "",
        "## Commands (each --approve needs an explicit human yes)",
        "",
        "```bash",
        *preview["commands"],
        "```",
        "",
        f"_{preview['note']}_",
        "",
    ]
    return "\n".join(lines)
