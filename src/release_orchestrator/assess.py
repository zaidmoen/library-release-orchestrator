"""Unified, read-only release assessment for ``/library-release`` (ADR-0016, Phase 1).

One call answers: *can this ship, why, and what is the most useful next step?* It only
**composes** existing engine results (no new analysis):

``discover_context`` -> ``ModeEngine.discover`` -> ``ChangeResolver.resolve`` ->
``ModeEngine.options`` (Q1, built on ``build_plan``) -> ``build_release_graph``.

Coverage is never reduced: the selected change is checked against **every** unreleased
change in ``base..source`` by the existing pairwise analysis (transitively, to a
fixpoint), including undeclared code dependencies and commits without a ticket.

Status (deterministic):

* ``BLOCKED``: a blocking condition the engine confirmed (static missing symbol, Jira
  "blocked by" policy, cycle, order conflict, environment/policy refusal);
* ``UNKNOWN``: the change to assess, or a required fact, can't be established;
* ``NEEDS REVIEW``: nothing blocks, but evidence is incomplete, suspected, stale or not
  yet verified (for example no validation run for this exact plan);
* ``READY``: none of the above **and** every required evidence item is present and fresh.

Mutable state (refs, remote tags, PR reviews/checks, Jira, registry, environments) is
read live in this run and never taken from the analysis cache. Content changes (adding
a prerequisite, reordering, holding) are only ever *proposed*; nothing is decided here.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from release_orchestrator.context import RepoContext
from release_orchestrator.errors import OrchestratorError
from release_orchestrator.gitutil.repo import NETWORK, STATS
from release_orchestrator.integrations.jira import NullIssueTracker
from release_orchestrator.integrations.linkage import ChangeResolver, PullSource
from release_orchestrator.modes import ModeEngine
from release_orchestrator.release_graph import build_release_graph

READY, BLOCKED, NEEDS_REVIEW, UNKNOWN = "READY", "BLOCKED", "NEEDS REVIEW", "UNKNOWN"
ICON = {READY: "🟢", BLOCKED: "🔴", NEEDS_REVIEW: "🟡", UNKNOWN: "⚪"}
_INCOMPLETE_KINDS = {"UNVERIFIED_FILE", "PARSE_ERROR", "AMBIGUOUS_COMMIT", "MERGE_COMMIT_SKIPPED"}
_STATIC_KINDS = {"MISSING_PREREQUISITE", "DIRECT_CODE_DEPENDENCY"}


def assess(
    engine: ModeEngine,
    context: RepoContext,
    *,
    pulls: PullSource | None,
    pr: str | None = None,
    ticket: str | None = None,
    candidate: str | None = None,
    order: list[str] | None = None,
    sheet: Any = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    git0, gits0, net0, netg0 = STATS.calls, STATS.seconds, NETWORK.seconds, NETWORK.git_seconds
    out = _Assessment(engine, context, pulls=pulls, sheet=sheet)
    payload = out.run(pr=pr, ticket=ticket, candidate=candidate, order=order)
    total = time.perf_counter() - started
    git_local = (STATS.seconds - gits0) - (NETWORK.git_seconds - netg0)
    network = NETWORK.seconds - net0
    payload["timings"] = {
        "total_seconds": round(total, 3),
        "engine_seconds": round(max(total - git_local - network, 0.0), 3),
        "git_seconds": round(git_local, 3),
        "network_seconds": round(network, 3),
        "git_subprocess_calls": STATS.calls - git0,
    }
    return payload


class _Assessment:
    def __init__(
        self, engine: ModeEngine, context: RepoContext, *, pulls: PullSource | None, sheet: Any
    ) -> None:
        self.engine, self.ctx, self.pulls, self.sheet = engine, context, pulls, sheet
        self.config = engine.config
        self.log: list[dict[str, str]] = []
        self.why: list[str] = []
        self.next: list[str] = []
        self.review: list[str] = []
        self.questions: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ helpers
    def fact(self, name: str, value: str, source: str) -> None:
        self.log.append({"fact": name, "value": value, "source": source})

    def payload(self, status: str, headline: str, **extra: Any) -> dict[str, Any]:
        return {
            "status": status,
            "icon": ICON[status],
            "headline": headline,
            "why": self.why,
            "next": self.next,
            "review": self.review,
            "decisions_needed": self.questions,
            "decision_log": self.log,
            "freshness": self.freshness,
            "context": self.ctx.to_dict(),
            **extra,
            "note": "Read-only assessment: nothing was executed or changed.",
        }

    # ------------------------------------------------------------------ main flow
    def run(
        self,
        *,
        pr: str | None,
        ticket: str | None,
        candidate: str | None,
        order: list[str] | None,
    ) -> dict[str, Any]:
        engine = self.engine
        disc = engine.discover()
        self.freshness = self._freshness(disc)
        if self.ctx.repository:
            self.fact("repository", self.ctx.repository, self.ctx.repository_source)
        stable = disc["latest_stable"]
        if stable is None:
            self.why.append("No stable release tag exists, so nothing can be planned from it.")
            return self.payload(UNKNOWN, "No stable release found")
        self.fact("latest stable", stable["tag"], "git tags")

        if order:
            return self._reorder(order)
        if candidate:
            return self._release(candidate)

        resolver = ChangeResolver(
            git=engine.git,
            key_pattern=self.config.change_key_pattern,
            pulls=self.pulls,
            tracker=engine.orch.s.tracker,
            discovery=disc,
            repository=self.ctx.repository,
            default_branch=self.ctx.default_branch or self.config.library.source_ref,
        )
        res = resolver.resolve(pr=pr, ticket=ticket)
        pull = res.get("pull_request")
        if pull:
            self.fact(
                "pull request",
                f"#{pull['number']} {pull['title']}",
                f"{res['sources']['pull_requests']}",
            )
        if res["status"] == "CONFLICT":
            self.why += res["conflicts"]
            self.questions += res["questions"]
            self.next.append("Confirm which change you mean; nothing else is assumed.")
            return self.payload(
                UNKNOWN, "The PR and the ticket don't describe the same change", resolution=res
            )
        if res["status"] != "RESOLVED":
            self.questions += res["questions"]
            self.why += [q["question"] for q in res["questions"]]
            for q in res["questions"][:1]:
                self.next += [f"• {o}" for o in q["options"][:3]]
            headline = (
                "Which change should be released?"
                if res["scenario"] == "D:discover"
                else "The change to assess can't be identified yet"
            )
            return self.payload(UNKNOWN, headline, resolution=res)
        tickets = res["tickets"]
        where = "PR title/branch convention" if pull else "your input"
        self.fact("ticket(s)", ", ".join(tickets), where)
        self.review += [w for w in res["warnings"] if "not in the issue snapshot" in w]
        return self._release_candidate(tickets, res)

    # ------------------------------------------------------------------ modes
    def _release_candidate(self, tickets: list[str], res: dict[str, Any]) -> dict[str, Any]:
        engine, disc = self.engine, self.engine.discover()
        pending = {c["key"] for c in disc["pending_changes"]}
        released = [t for t in tickets if t not in pending]
        if released:
            change = next((c for c in res.get("changes", []) if c["key"] in released), None)
            where = change and change.get("released_in")
            self.why.append(
                f"{', '.join(released)} has no unreleased commits"
                + (f"; it is already in {where}." if where else " on the source branch.")
            )
            if len(released) == len(tickets):
                self.next.append("Nothing to release for this change.")
                if where:  # a fact, but not a release to approve
                    return self.payload(
                        NEEDS_REVIEW,
                        f"{', '.join(released)} is already released in {where}",
                        resolution=res,
                    )
                return self.payload(
                    UNKNOWN, f"{', '.join(released)} has nothing to release", resolution=res
                )
            tickets = [t for t in tickets if t in pending]
        self.fact("mode", "release-candidate", "the change isn't in a stable release yet")
        try:
            q1 = engine.options("release-candidate", tickets=tickets)
        except OrchestratorError as exc:
            self.why.append(exc.message)
            return self.payload(UNKNOWN, "The change can't be planned", resolution=res)
        graph = build_release_graph(engine, sheet=self.sheet, selection=tickets)
        opts = {o["id"]: o for o in q1["options"]}
        label = " + ".join(tickets)
        pr = res.get("pull_request")
        subject = f"PR #{pr['number']} · {label}" if pr else label
        exact = opts.get("requested")
        if exact and exact["valid"]:
            return self._ready_or_review(subject, exact, graph, res, tickets)
        return self._blocked(subject, label, opts, graph, res, tickets)

    def _release(self, candidate: str) -> dict[str, Any]:
        self.fact("mode", "release (finalize a candidate)", "a release tag was given")
        try:
            q1 = self.engine.options("release", candidate=candidate)
        except OrchestratorError as exc:
            self.why.append(exc.message)
            return self.payload(UNKNOWN, f"{candidate} can't be assessed")
        opts = [o for o in q1["options"] if o["id"].startswith("release:")]
        chosen = next((o for o in opts if o["id"].endswith(candidate)), None)
        if chosen is None:
            chosen = next((o for o in opts if o["recommended"]), None)
        if chosen is None:
            self.why.append("No pending release candidate matches.")
            return self.payload(UNKNOWN, f"{candidate} is not a pending release candidate")
        keys = sorted({k for s in chosen["steps"] for k in s["keys"]})
        graph = build_release_graph(self.engine, sheet=self.sheet, selection=keys)
        if not chosen["valid"]:
            self.why += chosen["evidence"][:3]
            self.next.append("Re-cut or reorder the candidates; the release can't be finalized.")
            return self.payload(BLOCKED, f"{candidate} can't be finalized", graph=_brief(graph))
        return self._ready_or_review(candidate, chosen, graph, None, keys)

    def _reorder(self, order: list[str]) -> dict[str, Any]:
        self.fact("mode", "reorder-releases", "an order was given")
        try:
            q1 = self.engine.options("reorder-releases", order=order)
        except OrchestratorError as exc:
            self.why.append(exc.message)
            return self.payload(UNKNOWN, "The requested order can't be assessed")
        graph = build_release_graph(self.engine, sheet=self.sheet, order=order)
        opts = {o["id"]: o for o in q1["options"]}
        req = opts.get("requested") or opts.get("dependency-order")
        subject = " → ".join(order)
        if req and req["valid"]:
            keys = sorted({k for s in req["steps"] for k in s["keys"]})
            return self._ready_or_review(subject, req, graph, None, keys)
        conflicts = (graph.get("requested_order") or {}).get("conflicts", [])
        self.why += conflicts[:2] or (req["evidence"][:2] if req else [])
        self.why += [n for n in (graph.get("requested_order") or {}).get("notes", [])][:1]
        alternatives = [o for o in q1["options"] if o["valid"] and o["id"] not in ("stop",)]
        requested = opts.get("requested") or {"steps": []}
        self._decision(
            "This order can't ship as requested. Choose how to proceed:",
            alternatives,
            fallback=None,
            requested_keys={k for st in requested["steps"] for k in st["keys"]},
            reorder=True,
        )
        return self.payload(BLOCKED, f"{subject} can't ship in this order", graph=_brief(graph))

    # ------------------------------------------------------------------ outcomes
    def _blocked(
        self,
        subject: str,
        label: str,
        opts: dict[str, Any],
        graph: dict[str, Any],
        res: dict[str, Any],
        tickets: list[str],
    ) -> dict[str, Any]:
        engine, stable = self.engine, self.engine.discover()["latest_stable"]
        plan, _ = engine._plan(tickets, stable["tag"], self.config.library.source_ref, "rc")
        findings = plan.analysis.get("findings", []) if plan else []
        static = [f for f in findings if f.get("kind") in _STATIC_KINDS]
        deps = [
            d for d in graph["dependencies"] if d["change"] in tickets and d["state"] != "released"
        ]
        for f in static[:2]:
            ev = f.get("evidence") or {}
            where = f" ({ev['file'].rsplit('/', 1)[-1]}:{ev['line']})" if ev.get("file") else ""
            needed = f.get("depends_on")
            rcs = _rcs_of(needed, engine.discover())
            self.why.append(
                f"{f['change']} uses {ev.get('symbol', 'a symbol')}{where}, which exists only "
                f"in {needed} (unreleased{'; in ' + ', '.join(rcs) if rcs else ''})."
            )
        evidence = []
        for d in deps:
            kinds = []
            if "code" in d["sources"]:
                kinds.append("missing symbol — static, confirmed")
            if "jira" in d["sources"]:
                kinds.append("Jira “blocked by” — declared")
            if "sheet" in d["sources"]:
                kinds.append("tracking sheet — declared")
            if "cherry-pick probe" in d["sources"]:
                kinds.append("cherry-pick conflict — suspected")
            evidence.append(f"{d['change']}→{d['requires']}: " + " · ".join(kinds))
        requested = opts.get("requested-only")
        blocking_deps = {
            b.get("depends_on")
            for st in (requested["steps"] if requested else [])
            for b in st.get("blockers", [])
        }
        by_edge = {(d["change"], d["requires"]): d for d in graph["dependencies"]}
        unknown_only = (
            not static
            and not graph["cycles"]
            and bool(blocking_deps)
            and None not in blocking_deps
            and all(
                by_edge.get((t, dep), {}).get("classification") == "UNKNOWN"
                for dep in blocking_deps
                for t in tickets
                if (t, dep) in by_edge
            )
            and any((t, dep) in by_edge for dep in blocking_deps for t in tickets)
        )
        if unknown_only:
            missing = ", ".join(sorted(str(d) for d in blocking_deps))
            self.why.append(
                f"Jira says {label} is blocked by {missing}, but {missing} has no commits and "
                "isn't in the issue snapshot. No code evidence either way; nothing is assumed."
            )
            self.next.append(
                f"Confirm in Jira whether {missing} is still required, or refresh the snapshot."
            )
            return self.payload(
                UNKNOWN,
                f"{label} compatibility can't be determined",
                resolution=res,
                graph=_brief(graph),
            )
        if not static:
            self.why += (requested["evidence"] if requested else [])[:2]
        self.why += [f"Evidence: {e}" for e in evidence[:2]]
        self.why.append("Runtime: not verified yet.")
        for cyc in graph["cycles"]:
            self.why.append(f"Dependency cycle: {' → '.join(cyc)}.")
        alternatives = [o for o in opts.values() if o["valid"] and o["id"] != "stop"]
        self._decision(
            f"No option ships {label} exactly as requested. Choose how to proceed:",
            alternatives,
            fallback=f"Hold {label} (fallback: does not meet the request)",
            requested_keys=set(tickets),
        )
        return self.payload(
            BLOCKED, f"{subject} can't be released on its own", resolution=res, graph=_brief(graph)
        )

    def _ready_or_review(
        self,
        subject: str,
        option: dict[str, Any],
        graph: dict[str, Any],
        res: dict[str, Any] | None,
        keys: list[str],
    ) -> dict[str, Any]:
        engine = self.engine
        steps = [s for s in option["steps"] if s["status"] not in ("NO_OP", "SKIPPED")]
        versions = " then ".join(s["version"] for s in steps) or "-"
        headline = f"{subject} can ship as requested → {versions}"
        # Most actionable first: missing validation evidence for this exact plan.
        validated = all(engine._validated(s["plan_id"]) for s in steps) and bool(steps)
        if not validated:
            self.review.append("this exact plan has no validation run yet")
            self.next.append("Validate it (runs the configured tests; needs your yes).")
        coupling: dict[str, set[str]] = {}
        for step in steps:
            plan, _ = engine._plan(step["keys"], step["base"], step["source"], step["prerelease"])
            findings = plan.analysis.get("findings", []) if plan else []
            gaps = sorted({f["kind"] for f in findings if f.get("kind") in _INCOMPLETE_KINDS})
            if gaps:
                self.review.append(
                    "Static analysis is incomplete for this change ("
                    + ", ".join(gaps)
                    + "), so independence isn't proven."
                )
            for f in findings:
                if f.get("kind") == "SHARED_SYMBOL_MODIFIED" and f.get("depends_on"):
                    symbol = (f.get("evidence") or {}).get("symbol", "a shared symbol")
                    coupling.setdefault(symbol, set()).add(str(f["depends_on"]))
        for symbol, others in sorted(coupling.items()):
            self.review.append(
                f"behaviour coupling (warning): {', '.join(sorted(others))} also modify "
                f"{symbol}, which this change uses"
            )
        overlaps = [o for o in graph.get("overlaps", []) if o.get("review")]
        self.review += [o["note"] for o in overlaps]
        self.review += [
            f"{d['change']} may require {d['requires']} ({d['classification']}; "
            f"{', '.join(d['sources'])})"
            for d in graph["dependencies"]
            if d["change"] in keys
            and d["classification"] in ("INFERRED", "UNKNOWN")
            and d["state"] in ("pending", "missing")
        ]
        self.review += [f"Conflict: {c}" for c in graph["conflicts"]]
        self.review += [f"Uncertain: {u}" for u in self.engine.discover()["uncertainties"]]
        if self.freshness.get("issue_tracker_stale"):
            self.review.append(self.freshness["issue_tracker_note"])
        self.why.append(
            "No code or Jira dependency on unreleased work"
            " (checked against every unreleased change, including undeclared ones)."
        )
        approval = engine._approval_status(steps, engine.discover()["approval_policy"])
        if approval.get("required") and not approval.get("verified"):
            self.review.append(f"Approvals: {approval.get('detail', 'not verified')}.")
        if self.review:
            more = f" (+{len(self.review) - 1} more; say “why”)" if len(self.review) > 1 else ""
            self.why.append(f"Not READY: {self.review[0]}{more}.")
            return self.payload(NEEDS_REVIEW, headline, resolution=res, graph=_brief(graph))
        self.why.append("Validation passed on this plan; evidence read live in this run.")
        self.next.append("Prepare the release branch (write: needs your yes).")
        return self.payload(READY, headline, resolution=res, graph=_brief(graph))

    def _decision(
        self,
        question: str,
        options: list[dict[str, Any]],
        fallback: str | None,
        *,
        requested_keys: set[str],
        reorder: bool = False,
    ) -> None:
        """Offer content decisions. Each says exactly what it changes vs the request."""
        choices = []
        for o in options[:2]:
            produces = ", then ".join(
                f"{p['candidate'] + ' → ' if p['candidate'] else ''}{p['version']}"
                for p in o.get("produces", [])
                if p["status"] not in ("NO_OP", "SKIPPED")
            )
            shipped = {k for st in o["steps"] for k in st["keys"]}
            added = sorted(shipped - requested_keys)
            if reorder:
                change = (
                    "retires the older candidates instead of releasing them"
                    if o["id"] == "supersede"
                    else "changes the requested order"
                )
            else:
                change = f"adds {', '.join(added)}" if added else ""
            scope = f" ({change}: your decision)" if change else ""
            choices.append(
                {
                    "id": o["id"],
                    "label": o["label"],
                    "description": f"{produces}{scope}",
                    "changes_scope": bool(change),
                    "verified": "static analysis only; runtime not verified",
                }
            )
            self.next.append(f"• {o['label']}: {produces}{scope}")
        if fallback:
            self.next.append(f"• {fallback}")
            choices.append(
                {
                    "id": "hold",
                    "label": fallback,
                    "description": "does not meet the request",
                    "changes_scope": True,
                    "verified": "not verified",
                }
            )
        self.next.insert(0, "Decision needed (nothing is changed until you choose):")
        self.questions.append({"id": "remediation", "question": question, "options": choices})

    # ------------------------------------------------------------------ freshness
    def _freshness(self, disc: dict[str, Any]) -> dict[str, Any]:
        tracker = self.engine.orch.s.tracker
        out: dict[str, Any] = {
            "read_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "git": "read live in this run",
            "github": "verified"
            if (disc.get("github") or {}).get("verified")
            else "unavailable (no reachable remote)",
        }
        if isinstance(tracker, NullIssueTracker):
            out["issue_tracker"] = "not configured"
            return out
        fetched = tracker.fetched_at()
        out["issue_tracker"] = f"snapshot fetched_at {fetched}" if fetched else "snapshot (no date)"
        max_age = self.config.issue_max_age_minutes
        if fetched and max_age:
            age = (datetime.now(UTC) - fetched).total_seconds() / 60
            out["issue_tracker_stale"] = age > max_age
            out["issue_tracker_note"] = (
                f"Jira snapshot is {int(age)} min old (max {max_age}); refresh it."
            )
        return out


def _rcs_of(key: str | None, disc: dict[str, Any]) -> list[str]:
    if not key:
        return []
    change = next((c for c in disc["pending_changes"] if c["key"] == key), None)
    return list(change["in_candidates"]) if change else []


def _brief(graph: dict[str, Any]) -> dict[str, Any]:
    return {
        "dependencies": graph["dependencies"],
        "overlaps": graph.get("overlaps", []),
        "ancestry": graph["ancestry"],
        "missing_prerequisites": graph["missing_prerequisites"],
        "recommended_order": [o["candidate"] for o in graph["recommended_order"]],
        "requested_order": graph.get("requested_order"),
        "eligibility": graph["eligibility"],
    }


def render_brief(payload: dict[str, Any]) -> str:
    """The default output: STATUS / WHY / NEXT, at most 8 lines."""
    lines = [f"{payload['icon']} {payload['status']} — {payload['headline']}"]
    why = payload["why"][:3]
    lines += [("WHY   " if i == 0 else "      ") + w for i, w in enumerate(why)]
    nxt = payload["next"][:3]
    lines += [("NEXT  " if i == 0 else "      ") + n for i, n in enumerate(nxt)]
    hint = 'Say "why" for evidence'
    if payload["status"] == BLOCKED:
        hint += ", or choose an option"
    lines.append(f"      {hint}.")
    return "\n".join(lines[:8]) + "\n"
