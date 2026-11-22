"""Release ordering and dependency intelligence (ADR-0015).

Two different relations, never mixed:

* **Ancestry** (``contains``): release B's commits include release A's. This is a fact
  about *content*, read from Git (``merge-base --is-ancestor``). It is **never** treated as
  proof that B's change needs A's change to work.
* **Functional dependency** (``requires``): change B needs change A. Only explicit
  evidence creates one:

  ============  ======================================================  ==============
  source        evidence                                                classification
  ============  ======================================================  ==============
  jira          issue link "is blocked by"                              CONFIRMED
  code          B uses a symbol that only A introduces (AST analysis)   CONFIRMED
  sheet         dependency-tracking sheet row (declaration)             CONFIRMED, or
                                                                        INFERRED when
                                                                        Jira disagrees
  probe         cherry-pick of B fails without A                        INFERRED
  overlap       B and A touch the same file/symbol (undirected; listed  INFERRED
                under ``overlaps``, never a dependency or a cycle)
  ============  ======================================================  ==============

  A dependency on a ticket that no source knows (no commits, no issue) is ``UNKNOWN``. An
  edge on a cycle, or a confirmed prerequisite that is unreleased and outside the
  selection, is ``BLOCKED``.

Source precedence (conflicts are *reported*, never silently resolved):

1. Git (and GitHub): what each release contains, and the release order of tags.
2. Jira: functional links. A sheet row Jira doesn't have is a conflict -> INFERRED.
3. Code analysis: independent proof. A code dependency without a Jira link is CONFIRMED
   and flagged as "missing Jira link".
4. Dependency sheet: supplementary declarations and release assignments; a release
   assignment Git contradicts is a conflict.
5. YAML config: policies and mappings only, never release state.

INFERRED or UNKNOWN relationships never make a plan READY: they yield NEEDS REVIEW.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from release_orchestrator.analysis.graph import ChangeGraph
from release_orchestrator.integrations.dependency_sheet import SheetSnapshot
from release_orchestrator.integrations.jira import NullIssueTracker

CONFIRMED, INFERRED, UNKNOWN, BLOCKED = "CONFIRMED", "INFERRED", "UNKNOWN", "BLOCKED"
_CODE_PROOF = {"MISSING_PREREQUISITE", "DIRECT_CODE_DEPENDENCY"}
_PROBE = {"GIT_CONFLICT"}  # directed: the change does not apply without the other
_OVERLAP = {"SHARED_FILE", "SHARED_SYMBOL_MODIFIED"}  # undirected: they touch the same code


@dataclass
class Edge:
    change: str
    requires: str
    classification: str
    sources: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    state: str = ""  # released | in-selection | pending | missing | cycle
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_release_graph(
    engine: Any,  # ModeEngine (loosely typed to avoid an import cycle)
    *,
    sheet: SheetSnapshot | None = None,
    selection: list[str] | None = None,
    order: list[str] | None = None,
) -> dict[str, Any]:
    disc = engine.discover()
    git, config = engine.git, engine.config
    tracker = engine.orch.s.tracker
    stable = disc["latest_stable"]
    source_ref = config.library.source_ref
    source_sha = disc["source"]["sha"]
    cands = {c["tag"]: c for c in disc["pending_candidates"]}
    pending = {c["key"]: c for c in disc["pending_changes"]}
    selected = set(selection) if selection is not None else None
    anomalies: list[dict[str, str]] = []
    conflicts: list[str] = []
    warnings: list[str] = []

    # ---------------------------------------------------------------- releases + ancestry
    releases = []
    ancestry = []
    for tag, cand in cands.items():
        on_stable = bool(stable) and git.is_ancestor(stable["sha"], cand["sha"])
        on_source = git.is_ancestor(cand["sha"], source_sha)
        releases.append(
            {
                "tag": tag,
                "version": cand["version"],
                "tickets": cand["keys"],
                "own_tickets": cand["own_keys"],
                "builds_on_stable": on_stable,
                "on_source_branch": on_source,
                "published": cand["published"],
            }
        )
        for inner in cand["contains"]:
            ancestry.append(
                {
                    "release": tag,
                    "contains": inner,
                    "evidence": f"git: {inner} is an ancestor of {tag}",
                    "functional": False,
                }
            )
        if stable and not on_stable:
            anomalies.append(
                {
                    "kind": "UNRELATED_BRANCH",
                    "subject": tag,
                    "detail": f"{tag} does not build on {stable['tag']}",
                }
            )
        if not on_source:
            anomalies.append(
                {
                    "kind": "NOT_ON_SOURCE_BRANCH",
                    "subject": tag,
                    "detail": f"{tag} is not an ancestor of {source_ref}; its order can't be "
                    "derived from the source history",
                }
            )
    tags = sorted(cands)
    for i, a in enumerate(tags):
        for b in tags[i + 1 :]:
            related = a in cands[b]["contains"] or b in cands[a]["contains"]
            shared = sorted(set(cands[a]["keys"]) & set(cands[b]["keys"]))
            if not related and shared:
                anomalies.append(
                    {
                        "kind": "AMBIGUOUS_ANCESTRY",
                        "subject": f"{a} / {b}",
                        "detail": f"both carry {', '.join(shared)} but neither contains the "
                        "other (cherry-picked or diverged); their order needs a human",
                    }
                )

    # ---------------------------------------------------------------- functional edges
    edges: dict[tuple[str, str], Edge] = {}

    def add(change: str, requires: str, source: str, evidence: str, cls: str) -> None:
        edge = edges.setdefault((change, requires), Edge(change, requires, cls))
        if source not in edge.sources:
            edge.sources.append(source)
        if evidence not in edge.evidence:
            edge.evidence.append(evidence)
        rank = {BLOCKED: 3, CONFIRMED: 2, INFERRED: 1, UNKNOWN: 0}
        if rank[cls] > rank[edge.classification]:
            edge.classification = cls

    overlaps: dict[tuple[str, str], dict[str, Any]] = {}

    def overlap(a: str, b: str, kind: str, evidence: str) -> None:
        """Heuristic, undirected: never a dependency, never a cycle, never a blocker."""
        pair = (min(a, b), max(a, b))
        item = overlaps.setdefault(
            pair, {"changes": list(pair), "kinds": [], "evidence": [], "classification": INFERRED}
        )
        if kind not in item["kinds"]:
            item["kinds"].append(kind)
        if evidence not in item["evidence"]:
            item["evidence"].append(evidence)

    tracker_on = not isinstance(tracker, NullIssueTracker)
    for key in pending:
        issue = tracker.get(key)
        for dep in issue.blocked_by if issue else ():
            add(key, dep, "jira", f"Jira: {key} is blocked by {dep}", CONFIRMED)
        if stable:
            step = engine._step(f"graph:{key}", [key], stable["tag"], source_ref, None)
            for b in step.blockers:
                dep = b.get("depends_on")
                if not dep or b.get("source") == "issue-tracker":
                    continue
                if b["reason"] in _CODE_PROOF:
                    add(key, dep, "code", f"code: {b['message']}", CONFIRMED)
                elif b["reason"] in _PROBE:
                    add(key, dep, "cherry-pick probe", f"probe: {b['message']}", INFERRED)
                elif b["reason"] in _OVERLAP:
                    overlap(key, dep, b["reason"], b["message"])
            plan, _ = engine._plan([key], stable["tag"], source_ref, None)
            for f in plan.analysis.get("findings", []) if plan else []:
                dep = f.get("depends_on")
                if dep and f.get("kind") in _OVERLAP and f.get("change") == key:
                    overlap(key, dep, str(f["kind"]), str(f.get("message", "")))

    if sheet is not None:
        for row in sheet.rows:
            for dep in row.depends_on:
                issue = tracker.get(row.ticket) if tracker_on else None
                jira_has = bool(issue and dep in issue.blocked_by)
                if tracker_on and issue is not None and issue.accessible and not jira_has:
                    conflicts.append(
                        f"sheet row {row.row}: {row.ticket} depends on {dep}, but Jira "
                        f"{row.ticket} has no such link"
                    )
                    add(row.ticket, dep, "sheet", f"sheet row {row.row} (Jira disagrees)", INFERRED)
                else:
                    add(row.ticket, dep, "sheet", f"sheet row {row.row}", CONFIRMED)
            for release, row_no in sheet.release_of(row.ticket):
                tag = release if release in cands else f"{config.library.tag_prefix}{release}"
                if tag in cands and row.ticket not in cands[tag]["keys"]:
                    conflicts.append(
                        f"sheet row {row_no}: {row.ticket} is assigned to {tag}, but Git shows "
                        f"{tag} doesn't contain it (Git wins for content)"
                    )
        warnings += [f"sheet: {e}" for e in sheet.errors]

    # code dependency without a Jira link: confirmed, but the tracker is incomplete
    for edge in edges.values():
        if "code" in edge.sources and "jira" not in edge.sources and tracker_on:
            edge.notes.append("missing Jira link: code proves this dependency")

    # ---------------------------------------------------------------- state per edge
    # Cycles are computed over evidence-backed edges only: an INFERRED guess must not be
    # able to block a release by closing a loop.
    graph = ChangeGraph(pending)
    for (change, requires), edge in edges.items():
        if edge.classification == CONFIRMED:
            graph.add_edge(change, requires)
    cyclic = {k for cycle in graph.cycles() for k in cycle}
    released_cache: dict[str, bool] = {}

    def released(key: str) -> bool:
        if key not in released_cache:
            released_cache[key] = (
                bool(stable)
                and key_ok(key)
                and bool(git.first_commit_matching(stable["sha"], f"(^|[^A-Z0-9-]){key}([^0-9]|$)"))
            )
        return released_cache[key]

    for edge in edges.values():
        if edge.change in cyclic and edge.requires in cyclic:
            edge.state, edge.classification = "cycle", BLOCKED
        elif released(edge.requires):
            edge.state = "released"
        elif edge.requires in pending:
            edge.state = "in-selection" if selected and edge.requires in selected else "pending"
        else:
            edge.state = "missing"
            edge.notes.append(f"{edge.requires} has no commits on {source_ref}")
            if edge.classification == CONFIRMED:
                edge.classification = UNKNOWN if tracker.get(edge.requires) is None else BLOCKED
        if (
            selected is not None
            and edge.change in selected
            and edge.state == "pending"
            and edge.classification == CONFIRMED
        ):
            edge.classification = BLOCKED
            edge.notes.append(f"{edge.requires} is required but not selected")

    edge_list = sorted(edges.values(), key=lambda e: (e.change, e.requires))
    relevant = [e for e in edge_list if selected is None or e.change in selected]
    # With a selection: prerequisites of selected changes that are neither released nor
    # selected. Without one: dependencies on changes that exist nowhere.
    missing = sorted(
        {
            e.requires
            for e in relevant
            if (
                e.state in ("pending", "missing")
                if selected is not None and e.change in selected
                else e.state == "missing"
            )
        }
    )
    order_report = _order(order, cands, edge_list, released) if order else None
    recommended = _recommended(cands, edge_list)
    review = [
        e
        for e in relevant
        if e.classification in (INFERRED, UNKNOWN) and e.state in ("pending", "missing")
    ]
    position = {k: i for i, k in enumerate(pending)}
    overlap_list = sorted(overlaps.values(), key=lambda o: o["changes"])
    for item in overlap_list:
        a, b = item["changes"]
        item["review"] = False
        if selected is None:
            continue
        for mine, other in ((a, b), (b, a)):
            # only an *earlier*, unselected, unreleased change can be what "mine" was
            # built on; later changes are irrelevant to releasing "mine"
            if (
                mine in selected
                and other not in selected
                and other in position
                and mine in position
                and position[other] < position[mine]
                and not released(other)
            ):
                item["review"] = True
                item["note"] = f"{mine} overlaps earlier unselected {other}: needs review"
    blocked = [e for e in relevant if e.classification == BLOCKED]
    blocked_order = bool(order_report and order_report["conflicts"])
    eligibility = (
        BLOCKED
        if blocked or blocked_order
        else "NEEDS REVIEW"
        if review
        or conflicts
        or any(o["review"] for o in overlap_list)
        or any(a["kind"] != "NOT_ON_SOURCE_BRANCH" for a in anomalies)
        else "READY"
    )
    return {
        "library": config.library.name,
        "latest_stable": stable,
        "releases": releases,
        "finalized_candidates": disc["finalized_candidates"],
        "ancestry": ancestry,
        "dependencies": [e.to_dict() for e in edge_list],
        "overlaps": overlap_list,
        "selection": sorted(selected) if selected is not None else None,
        "missing_prerequisites": missing,
        "cycles": graph.cycles(),
        "anomalies": anomalies,
        "conflicts": conflicts,
        "warnings": warnings,
        "recommended_order": recommended,
        "requested_order": order_report,
        "eligibility": eligibility,
        "evidence_sources": {
            "git": f"tags and ancestry in {config.library.repo.name}",
            "jira": "issue snapshot" if tracker_on else "not configured (functional links UNKNOWN)",
            "code": "AST symbol analysis (CONFIRMED); cherry-pick probe and shared files "
            "(INFERRED)",
            "sheet": sheet.source if sheet else "not configured",
        },
        "mermaid": _mermaid(stable, cands, ancestry, edge_list),
        "note": "Ancestry (contains) is never used as proof of a functional dependency.",
    }


def key_ok(key: str) -> bool:
    return bool(key) and all(ch.isalnum() or ch in "-_" for ch in key)


def _owner(key: str, cands: dict[str, Any]) -> list[str]:
    """Candidates whose *own* changes include ``key``."""
    return [t for t, c in cands.items() if key in c["own_keys"]]


def _recommended(cands: dict[str, Any], edges: list[Edge]) -> list[dict[str, Any]]:
    """Candidates ordered by functional dependencies first, then ancestry, then version."""
    order: list[dict[str, Any]] = []
    remaining = sorted(cands, key=lambda t: (len(cands[t]["contains"]), t))
    placed: set[str] = set()
    while remaining:
        for tag in remaining:
            needs = {
                o
                for e in edges
                if e.change in cands[tag]["own_keys"]
                for o in _owner(e.requires, cands)
                if o != tag
            }
            inner = set(cands[tag]["contains"])
            if (needs | inner) <= placed:
                why = [f"functional: needs {', '.join(sorted(needs))}"] if needs else []
                why += [f"ancestry: contains {', '.join(sorted(inner))}"] if inner else []
                order.append({"candidate": tag, "after": sorted(needs | inner), "why": why})
                placed.add(tag)
                remaining.remove(tag)
                break
        else:  # unresolvable (cycle between candidates): keep version order, flag it
            for tag in remaining:
                order.append({"candidate": tag, "after": [], "why": ["UNORDERED: cycle"]})
            break
    return order


def _order(
    order: list[str], cands: dict[str, Any], edges: list[Edge], released: Any
) -> dict[str, Any]:
    by_version = {c["version"]: t for t, c in cands.items()}
    order = [by_version.get(o, o) for o in order]
    conflicts, notes = [], []
    for i, tag in enumerate(order):
        if tag not in cands:
            conflicts.append(f"{tag} is not a pending candidate")
            continue
        later = set(order[i + 1 :])
        for e in edges:
            if e.change not in cands[tag]["own_keys"] or released(e.requires):
                continue
            owners = _owner(e.requires, cands)
            if owners and set(owners) <= later and e.classification in (CONFIRMED, BLOCKED):
                conflicts.append(
                    f"{tag} before {', '.join(owners)}: {e.change} requires {e.requires} "
                    f"({'; '.join(e.evidence)})"
                )
            elif owners and set(owners) <= later:
                notes.append(
                    f"{tag} before {', '.join(owners)}: {e.change} may require {e.requires} "
                    f"({e.classification}); needs review"
                )
        for inner in cands[tag]["contains"]:
            if inner in later:
                notes.append(
                    f"{tag} contains {inner}'s commits (ancestry): releasing {tag} first ships "
                    f"{inner}'s changes too. This is content, not a functional dependency."
                )
    return {"order": order, "conflicts": conflicts, "notes": notes, "valid": not conflicts}


def _mermaid(
    stable: dict[str, str] | None,
    cands: dict[str, Any],
    ancestry: list[dict[str, Any]],
    edges: list[Edge],
) -> str:
    def node(name: str) -> str:
        return "n_" + "".join(ch if ch.isalnum() else "_" for ch in name)

    lines = ["graph LR"]
    if stable:
        lines.append(f'  {node(stable["tag"])}["{stable["tag"]} (stable)"]')
    for tag, cand in sorted(cands.items()):
        lines.append(f'  {node(tag)}["{tag}"]')
        if stable and not cand["contains"]:
            lines.append(f"  {node(stable['tag'])} -.base.-> {node(tag)}")
        for key in cand["own_keys"]:
            lines.append(f"  {node(tag)} --- {node(key)}([{key}])")
    for a in ancestry:
        lines.append(f"  {node(a['contains'])} -.contains.-> {node(a['release'])}")
    for e in edges:
        lines.append(f"  {node(e.change)} ==>|requires {e.classification}| {node(e.requires)}")
    return "\n".join(lines)
