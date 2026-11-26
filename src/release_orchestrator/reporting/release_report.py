"""Dry-run release report and Slack-ready summary (ADR-0015).

Inputs are the read-only results the Skill already has: repository context, PR/Jira
resolution, impact preview and dependency graph. The report adds **no new decisions**: it
combines them and derives one final eligibility:

* ``BLOCKED``: the preview or the dependency graph is blocked (missing confirmed
  prerequisite, cycle, conflicting order, blocked environment...);
* ``NEEDS REVIEW``: nothing is blocked, but something needs a human: validation evidence
  missing, INFERRED/UNKNOWN relationships, source conflicts, unanswered questions,
  context problems or discovery uncertainties;
* ``READY``: none of the above. READY still means *eligible for the approval gates*, never
  "executed" or "approved".

Slack delivery is optional and separate: ``render_slack`` only builds a payload. Sending
needs ``SlackWebhookDelivery.send(..., approve=True)``, a webhook read from an environment
variable, and an explicit human yes. Nothing here sends anything by default.
"""

from __future__ import annotations

import json
import os
import urllib.request
from collections.abc import Callable
from typing import Any

from release_orchestrator.errors import ApprovalRequiredError, ConfigError, IntegrationError

READY, BLOCKED, NEEDS_REVIEW = "READY", "BLOCKED", "NEEDS REVIEW"
_ICON = {
    READY: ":large_green_circle:",
    BLOCKED: ":red_circle:",
    NEEDS_REVIEW: ":large_yellow_circle:",
}


def build_report(
    *,
    context: dict[str, Any],
    preview: dict[str, Any],
    graph: dict[str, Any],
    linkage: dict[str, Any] | None = None,
    discovery: dict[str, Any] | None = None,
) -> dict[str, Any]:
    blocked: list[str] = []
    review: list[str] = []
    if preview["readiness"] == "BLOCKED":
        blocked += [f"preview: {b}" for b in preview["blockers"]] or ["preview is BLOCKED"]
    elif preview["readiness"] != "READY":
        review.append(f"preview readiness is {preview['readiness']}")
    for edge in graph["dependencies"]:
        if graph["selection"] is not None and edge["change"] not in graph["selection"]:
            continue
        line = (
            f"{edge['change']} requires {edge['requires']} [{edge['classification']}; "
            f"{', '.join(edge['sources'])}; {edge['state']}]"
        )
        if edge["classification"] == "BLOCKED":
            blocked.append(line)
        elif edge["classification"] in ("INFERRED", "UNKNOWN"):
            review.append(line)
    review += [o["note"] for o in graph.get("overlaps", []) if o.get("review")]
    if graph.get("requested_order") and graph["requested_order"]["conflicts"]:
        blocked += [f"order: {c}" for c in graph["requested_order"]["conflicts"]]
    blocked += [f"cycle: {' -> '.join(c)}" for c in graph["cycles"]]
    conflicts = list(graph["conflicts"])
    questions: list[str] = []
    if linkage is not None:
        conflicts += linkage["conflicts"]
        questions += [q["question"] for q in linkage["questions"]]
        if linkage["status"] != "RESOLVED":
            review.append(f"PR/Jira resolution is {linkage['status']}")
    review += [f"conflict: {c}" for c in conflicts]
    review += [f"unanswered: {q}" for q in questions]
    review += [f"context: {p['message']}" for p in context.get("problems", [])]
    uncertainties = (discovery or {}).get("uncertainties", [])
    review += [f"uncertain: {u}" for u in uncertainties]
    review += [f"{a['kind']}: {a['detail']}" for a in graph["anomalies"]]
    eligibility = BLOCKED if blocked else NEEDS_REVIEW if review else READY

    pr = (linkage or {}).get("pull_request")
    stable = preview["latest_stable"]
    return {
        "title": f"Release dry run: {graph['library']} ({preview['mode']})",
        "eligibility": eligibility,
        "blocked_reasons": blocked,
        "review_reasons": review,
        "repository": {
            "name": context.get("repository") or "unresolved",
            "source": context.get("repository_source"),
            "branch": context.get("branch"),
            "head": (context.get("head") or "")[:12],
            "default_branch": context.get("default_branch"),
        },
        "source_change": {
            "pull_request": (
                {
                    "number": pr["number"],
                    "title": pr["title"],
                    "url": pr["url"],
                    "data": pr["source"],
                }
                if pr
                else None
            ),
            "tickets": (linkage or {}).get("tickets")
            or sorted({k for s in preview["release_steps"] for k in s["tickets"]}),
            "resolution": (linkage or {}).get("status", "not requested"),
        },
        "mode": preview["mode"],
        "choice": preview["choice"],
        "latest_stable": stable,
        "pending_candidates": preview["pending_candidates"],
        "release_steps": preview["release_steps"],
        "proposed_version": preview["proposed_version"],
        "dependency_graph": {
            "ancestry": graph["ancestry"],
            "dependencies": graph["dependencies"],
            "overlaps": graph.get("overlaps", []),
            "recommended_order": graph["recommended_order"],
            "requested_order": graph.get("requested_order"),
            "mermaid": graph["mermaid"],
        },
        "missing_prerequisites": graph["missing_prerequisites"],
        "affected_services": preview["affected_services"],
        "services": preview["services"],
        "environments": [e for e in preview["environment_mapping"] if e["selected"]],
        "environment_mapping": preview["environment_mapping"],
        "evidence_sources": {
            **graph["evidence_sources"],
            **({"pull_requests": linkage["sources"]["pull_requests"]} if linkage else {}),
            "github_releases": ((discovery or {}).get("github") or {})
            .get("releases", {})
            .get("read", False),
        },
        "conflicts": conflicts,
        "warnings": list(preview["warnings"]) + list(graph["warnings"]),
        "uncertainties": uncertainties,
        "questions": questions,
        "preview_id": preview.get("preview_id"),
        "fingerprint": preview.get("fingerprint"),
        "dry_run": "Nothing was executed: no branch, tag, PR, publish, Jira or environment change.",
    }


def render_markdown(report: dict[str, Any]) -> str:
    repo, src = report["repository"], report["source_change"]
    stable = report["latest_stable"]
    pr = src["pull_request"]
    lines = [
        f"# {report['title']}",
        "",
        f"**Final eligibility: {report['eligibility']}**. {report['dry_run']}",
        "",
        f"* Repository: `{repo['name']}` (from {repo['source']}); branch `{repo['branch']}` "
        f"at `{repo['head']}`; default branch `{repo['default_branch']}`",
        "* Source: "
        + (f"PR #{pr['number']} {pr['title']} ({pr['data']})" if pr else "no PR")
        + f"; tickets {', '.join(src['tickets']) or 'none'}; resolution {src['resolution']}",
        f"* Mode: {report['mode']}; choice **{report['choice']['label']}**",
        f"* Latest stable: {stable['version']} (`{stable['tag']}`, {stable['sha'][:10]})",
        f"* Proposed version: **{report['proposed_version']}**",
        "",
        "## Pending release candidates",
        "",
        "| Candidate | Tickets | Contains (ancestry) | Own tickets |",
        "|---|---|---|---|",
    ]
    lines += [
        f"| {c['tag']} | {', '.join(c['keys'])} | {', '.join(c['contains']) or '-'} | "
        f"{', '.join(c['own_keys'])} |"
        for c in report["pending_candidates"]
    ] or ["| none | | | |"]
    lines += [
        "",
        "## Proposed releases",
        "",
        "| Step | Candidate | Tickets | Version | Status |",
        "|---|---|---|---|---|",
    ]
    lines += [
        f"| {s['step']} | {s['candidate'] or '-'} | {', '.join(s['tickets']) or '-'} | "
        f"{s['version']} | {s['status']} |"
        for s in report["release_steps"]
    ]
    graph = report["dependency_graph"]
    lines += ["", "## Dependency graph", "", "Functional dependencies (evidence-backed):", ""]
    lines += [
        f"* {d['change']} requires {d['requires']}: **{d['classification']}** "
        f"({', '.join(d['sources'])}; {d['state']}) {'; '.join(d['notes'])}".rstrip()
        for d in graph["dependencies"]
    ] or ["* none found"]
    lines += ["", "Overlaps (heuristic, INFERRED; never a dependency):", ""]
    lines += [
        f"* {' / '.join(o['changes'])}: {', '.join(o['kinds'])}"
        + (" — **needs review**" if o.get("review") else "")
        for o in graph.get("overlaps", [])
    ] or ["* none"]
    lines += ["", "Ancestry (content only, never a dependency):", ""]
    lines += [f"* {a['release']} contains {a['contains']}" for a in graph["ancestry"]] or ["* none"]
    lines += ["", "```mermaid", graph["mermaid"], "```"]
    if graph.get("requested_order"):
        ro = graph["requested_order"]
        lines += [
            "",
            f"Requested order {' -> '.join(ro['order'])}: "
            f"{'valid' if ro['valid'] else 'CONFLICTS'}",
        ]
        lines += [f"* conflict: {c}" for c in ro["conflicts"]] + [
            f"* note: {n}" for n in ro["notes"]
        ]
    lines += [
        "",
        "Recommended order: " + " -> ".join(o["candidate"] for o in graph["recommended_order"]),
    ]
    lines += ["", "## Blocked prerequisites", ""]
    lines += [f"* {k}" for k in report["missing_prerequisites"]] or ["* none"]
    lines += [
        "",
        "## Services",
        "",
        "| Service | Current | Target | Status | Notes |",
        "|---|---|---|---|---|",
    ]
    lines += [
        f"| {s['service']} | {s['current_version'] or '-'} | {s['target_version'] or '-'} | "
        f"{s['status']} | {s.get('notes', '')} |"
        for s in report["services"]
    ]
    lines += ["", "## Target environments", ""]
    lines += [f"* {e['environment']} (branch `{e['branch']}`)" for e in report["environments"]] or [
        "* none selected (no environment operation)"
    ]
    lines += ["", "## Evidence sources", ""]
    lines += [f"* {k}: {v}" for k, v in report["evidence_sources"].items()]
    for title, key in (
        ("Blocked because", "blocked_reasons"),
        ("Needs review", "review_reasons"),
        ("Warnings", "warnings"),
    ):
        if report[key]:
            lines += ["", f"## {title}", ""] + [f"* {x}" for x in report[key]]
    return "\n".join(lines) + "\n"


def _clip(text: str, limit: int = 2900) -> str:
    return text if len(text) <= limit else text[: limit - 20] + "\n...(truncated)"


def render_slack(report: dict[str, Any]) -> dict[str, Any]:
    """Slack Block Kit payload (``text`` is the notification fallback)."""
    status = report["eligibility"]
    repo, src = report["repository"], report["source_change"]
    pr = src["pull_request"]
    steps = "\n".join(
        f"{s['step']}. {s['candidate'] or 'new RC'} -> `{s['version']}` ({s['status']})"
        for s in report["release_steps"]
    )
    deps = (
        "\n".join(
            f"• {d['change']} → {d['requires']}: *{d['classification']}* "
            f"({', '.join(d['sources'])})"
            for d in report["dependency_graph"]["dependencies"]
            if not report["source_change"]["tickets"]
            or d["change"] in report["source_change"]["tickets"]
            or d["requires"] in report["source_change"]["tickets"]
        )
        or "• none"
    )
    services = "\n".join(
        f"• {s['service']}: {s['current_version'] or '-'} → {s['target_version'] or '-'} "
        f"({s['status']})"
        for s in report["services"]
    )
    envs = (
        ", ".join(f"{e['environment']} ({e['branch']})" for e in report["environments"]) or "none"
    )
    reasons = report["blocked_reasons"] if status == BLOCKED else report["review_reasons"]
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": _clip(report["title"], 150)}},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{_ICON[status]} *{status}* — dry run, nothing executed",
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Repository*\n`{repo['name']}`"},
                {"type": "mrkdwn", "text": f"*Mode*\n{report['mode']}"},
                {
                    "type": "mrkdwn",
                    "text": "*Source*\n"
                    + (
                        f"<{pr['url']}|PR #{pr['number']}>"
                        if pr and pr["url"]
                        else f"PR #{pr['number']}"
                        if pr
                        else "no PR"
                    )
                    + f" · {', '.join(src['tickets']) or 'no ticket'}",
                },
                {
                    "type": "mrkdwn",
                    "text": f"*Latest stable*\n`{report['latest_stable']['tag']}`",
                },
            ],
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": _clip(f"*Proposed releases*\n{steps}")},
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": _clip(f"*Dependencies*\n{deps}")}},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": _clip(f"*Microservices*\n{services}\n*Environments*: {envs}"),
            },
        },
    ]
    if reasons:
        title = "Why it is blocked" if status == BLOCKED else "Needs review"
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": _clip(f"*{title}*\n" + "\n".join(f"• {r}" for r in reasons[:12])),
                },
            }
        )
    data = report["source_change"]["pull_request"]
    label = f"PR data: {data['data']}" if data else "PR data: none"
    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"{label} · preview `{report.get('preview_id') or '-'}` · "
                    "approval gates still apply to every write",
                }
            ],
        }
    )
    return {"text": f"{status}: {report['title']}", "blocks": blocks}


def render_slack_text(report: dict[str, Any]) -> str:
    """Plain mrkdwn version for copy-paste (same content as the blocks)."""
    payload = render_slack(report)
    parts: list[str] = []
    for block in payload["blocks"]:
        if block["type"] == "header":
            parts.append(f"*{block['text']['text']}*")
        elif block["type"] == "section" and "text" in block:
            parts.append(block["text"]["text"])
        elif block["type"] == "section":
            parts.append(" | ".join(f["text"].replace("\n", ": ", 1) for f in block["fields"]))
        elif block["type"] == "context":
            parts.append("_" + block["elements"][0]["text"] + "_")
    return "\n\n".join(parts) + "\n"


Poster = Callable[[str, bytes], int]


def _urllib_post(url: str, body: bytes) -> int:
    request = urllib.request.Request(  # noqa: S310 (https enforced by the caller)
        url, data=body, method="POST", headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
            return int(response.status)
    except OSError as exc:
        raise IntegrationError(f"Slack delivery failed: {type(exc).__name__}") from None


class SlackWebhookDelivery:
    """Optional delivery adapter. Never used unless a human explicitly approves a send.

    The webhook URL is a secret: it's read from the named environment variable, only
    ``https://hooks.slack.com/`` is accepted, and it never appears in errors or output.
    """

    def __init__(self, webhook_env: str, *, post: Poster = _urllib_post) -> None:
        if not webhook_env.isidentifier():
            raise ConfigError("Slack webhook must be given as an environment variable name")
        self.webhook_env, self._post = webhook_env, post

    def send(self, payload: dict[str, Any], *, approve: bool) -> dict[str, Any]:
        if not approve:
            raise ApprovalRequiredError(
                "Sending to Slack posts to a real channel; it needs an explicit human yes",
                details={"webhook_env": self.webhook_env},
            )
        url = os.environ.get(self.webhook_env, "")
        if not url.startswith("https://hooks.slack.com/"):
            raise ConfigError(
                f"${self.webhook_env} is unset or not a https://hooks.slack.com/ webhook"
            )
        status = self._post(url, json.dumps(payload).encode())
        if status != 200:
            raise IntegrationError(f"Slack returned HTTP {status}")
        return {"sent": True, "status": status, "webhook_env": self.webhook_env}
