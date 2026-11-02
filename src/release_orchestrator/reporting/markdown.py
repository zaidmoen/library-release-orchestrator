"""Human-readable Markdown for plans, run reports and PR descriptions."""

from __future__ import annotations

import json
from typing import Any

from release_orchestrator.planning.plan import ReleasePlan


def _short(sha: str | None) -> str:
    return sha[:10] if sha else "-"


def render_plan(plan: ReleasePlan) -> str:
    v = plan.version
    lines = [
        f"# Release plan `{plan.plan_id}`: {plan.library} {plan.proposed_version}",
        "",
        f"**Status:** `{plan.status}`  ",
        f"**Verdict:** `{plan.analysis.get('verdict')}`  ",
        f"**Base:** `{plan.base_ref}` ({_short(plan.base_revision)}) · "
        f"**Source:** `{plan.source_ref}` ({_short(plan.source_revision)})  ",
        f"**Version:** {v.get('previous_version')} → **{plan.proposed_version}** "
        f"({v.get('bump')}, provisional)  ",
        f"**Release branch:** `{plan.branch}`",
        "",
        "## Included changes",
        "",
    ]
    for change in plan.included:
        lines.append(f"- **{change.key}**: {change.title}")
        lines += [f"  - `{_short(c.sha)}` {c.subject} ({c.author})" for c in change.commits]
    lines += ["", "## Excluded changes", ""]
    lines += [f"- **{c.key}**: {c.title} ({c.reason})" for c in plan.excluded] or ["- none"]
    if plan.already_released:
        lines += ["", f"Already released in base: {', '.join(plan.already_released)}"]

    findings = plan.analysis.get("findings", [])
    lines += ["", "## Dependency findings", ""]
    if findings:
        lines += ["| Severity | Kind | Change | Depends on | Detail |", "|---|---|---|---|---|"]
        lines += [
            f"| {f['severity']} | {f['kind']} | {f['change']} | {f.get('depends_on') or ''} "
            f"| {f['message']} |"
            for f in findings
        ]
    else:
        lines.append("No findings. Static analysis found no dependency on excluded changes.")
    probe = plan.analysis.get("probe") or {}
    if probe:
        lines += [
            "",
            f"Sandbox apply probe: {'clean' if probe.get('applied_cleanly') else 'CONFLICT'}"
            f" ({probe.get('note', '')})",
        ]

    if plan.blockers:
        lines += ["", "## Blockers", ""]
        lines += [f"- `{b['reason']}`: {b['message']}" for b in plan.blockers]
    if plan.remediation:
        lines += ["", "## Remediation options", ""]
        for r in plan.remediation:
            extra = f" → `{', '.join(r['changes'])}`" if r.get("changes") else ""
            lines.append(f"- **{r['option']}**{extra}: {r['description']}")

    lines += [
        "",
        "## Consumers",
        "",
        "| Service | Current | Proposed | Action | Why |",
        "|---|---|---|---|---|",
    ]
    lines += [
        f"| {c['name']} | {c.get('current_version') or '-'} | {c['proposed_version']} "
        f"| {c['action']} | {'; '.join(c.get('reasons', []))} |"
        for c in plan.consumers
    ]
    if plan.compatibility_risks:
        lines += ["", "## Compatibility risks", ""]
        lines += [f"- {r}" for r in plan.compatibility_risks]
    lines += ["", "## Validation required", ""]
    lines += [
        f"- `{r['scope']}` / {r['name']}: `{' '.join(r['argv'])}`" for r in plan.validation_required
    ] or ["- none configured"]
    lines += [
        "",
        "## Approval",
        "",
        "Human approval (`--approve`) is required for: prepare, pr, publish, "
        "update-consumers. Nothing is ever merged or deployed automatically.",
        "",
    ]
    return "\n".join(lines)


def render_run_report(plan: ReleasePlan, run: dict[str, Any]) -> str:
    lines = [
        render_plan(plan),
        "## Execution",
        "",
        f"**Run state:** `{run['state']}`",
        "",
        "| At | From | To | Note |",
        "|---|---|---|---|",
    ]
    lines += [f"| {h['at']} | {h['from']} | {h['to']} | {h['note']} |" for h in run["history"]]
    steps = run.get("steps", {})
    prepared = steps.get("prepare", {}).get("result", {})
    if prepared.get("provenance"):
        lines += ["", "### Provenance (source → release commit)", ""]
        lines += [
            f"- `{_short(p['source'])}` → `{_short(p['result'])}`" for p in prepared["provenance"]
        ]
    for key in ("validate", "publish"):
        if key in steps:
            lines += ["", f"### {key}", "", "```json", _json(steps[key]["result"]), "```"]
    for key in ("pr", "update-consumers"):
        if key in steps:
            lines += ["", f"### {key}", "", "```json", _json(steps[key]["result"]), "```"]
    return "\n".join(lines) + "\n"


def _json(data: Any) -> str:
    return json.dumps(data, indent=2)


def library_pr_body(plan: ReleasePlan, validation: dict[str, Any]) -> str:
    keys = ", ".join(plan.included_keys)
    excluded = ", ".join(c.key for c in plan.excluded) or "none"
    results = validation.get("results", [])
    checks = "\n".join(f"- {r['status']}: `{r['name']}` (exit {r['exit_code']})" for r in results)
    return (
        f"## Release {plan.library} {plan.proposed_version}\n\n"
        f"Prepared by Library Release Orchestrator from plan `{plan.plan_id}`.\n\n"
        f"- Base: `{plan.base_ref}` ({_short(plan.base_revision)})\n"
        f"- Included: {keys}\n- Excluded: {excluded}\n"
        f"- Dependency verdict: `{plan.analysis.get('verdict')}`\n\n"
        f"### Validation ({validation.get('status')})\n{checks or '- none'}\n\n"
        "### Commits\n"
        + "\n".join(f"- `{_short(c.sha)}` {c.subject}" for c in plan.included_commits)
        + "\n\n> This PR is a review vehicle for the release. Do not merge automatically. After "
        "approval, the release is published from the head of this branch as an immutable "
        "artifact.\n\n"
        f"Refs: {keys}\n"
    )


def consumer_pr_body(plan: ReleasePlan, consumer: str, update: dict[str, Any]) -> str:
    validation = update.get("validation") or {}
    return (
        f"## Bump {plan.library} {update.get('from_version')} → {plan.proposed_version}\n\n"
        f"Release plan `{plan.plan_id}` includes: {', '.join(plan.included_keys)}.\n"
        f"{consumer} was targeted because it is affected by or requested in these changes.\n\n"
        f"- Artifact: {plan.library} {plan.proposed_version} (published, immutable)\n"
        f"- Consumer validation against the published artifact: "
        f"**{validation.get('status', 'NOT RUN')}**\n"
        f"- Rollback: re-pin `{update.get('from_version')}`\n\n"
        "> Prepared automatically; review and merge are human decisions.\n\n"
        f"Refs: {', '.join(plan.included_keys)}\n"
    )
