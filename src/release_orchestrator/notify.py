"""Slack team visibility through Slack MCP: policy, threads and an idempotent outbox (ADR-0018).

The engine never talks to Slack. It decides *whether* and *what* to post, and records what
happened; Claude performs the one MCP call in between:

    notify plan   -> {"action": "post", channel_id, thread_ts, message}   (marks it pending)
    <slack_send_message>
    notify record -> delivered (ts from the response) | not_sent | uncertain

A pending or uncertain delivery is never re-posted blindly: ``notify plan`` answers
``reconcile`` and the channel/thread history must be read and passed to
``notify reconcile``, which looks for the delivery marker ``lro:<op>:<event>``.

Slack is notification-only. Nothing read from Slack (messages, reactions) is ever
approval or a release instruction.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from .errors import (
    AmbiguousRequestError,
    ApprovalRequiredError,
    InvalidInputError,
    NotFoundError,
)
from .operations import EVENTS, PHASE, OperationStore, _FileLock, _now, _write_atomic, redact

DEFAULT_CHANNEL = "library-release-orchestrator"
DEFAULT_EVENTS = list(EVENTS)
# Slack API errors that mean the message was definitely not posted (safe to retry later).
DEFINITE_FAILURES = (
    "channel_not_found",
    "not_in_channel",
    "is_archived",
    "missing_scope",
    "not_authed",
    "invalid_auth",
    "restricted_action",
    "msg_too_long",
    "no_text",
    "permission",
    "not allowed",
    "tool unavailable",
)
_PERMALINK = re.compile(r"/archives/([CG][A-Z0-9]{6,})/p(\d{10})(\d{6})")
_TS = re.compile(r"\b(\d{10}\.\d{6})\b")


def lro_home() -> Path:
    env = os.environ.get("LRO_HOME")
    return Path(env).expanduser() if env else Path.home() / ".lro"


def policy_path() -> Path:
    return lro_home() / "notify-policy.json"


def load_policy() -> dict[str, Any]:
    try:
        data = json.loads(policy_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"enabled": False}
    return dict(data)


# ------------------------------------------------------------------------------ channel
def resolve_channel(payload: str, name: str = DEFAULT_CHANNEL) -> dict[str, Any]:
    """The verified channel for ``name`` from a ``slack_search_channels`` result.

    Accepts the MCP's Markdown result (``Name: #x`` ... ``Permalink: .../archives/<ID>``)
    or JSON (``{"channels": [{"id", "name"}]}``). Exactly one non-archived exact-name match
    is required; a channel is never created or guessed.
    """
    found: list[dict[str, Any]] = []
    data: Any
    try:
        data = json.loads(payload)
    except ValueError:
        data = None
    text = payload
    if isinstance(data, dict) and isinstance(data.get("results"), str):
        text, data = data["results"], None
    if isinstance(data, dict | list):
        items = data.get("channels", []) if isinstance(data, dict) else data
        for c in items:
            if isinstance(c, dict) and c.get("id") and c.get("name"):
                found.append(
                    {
                        "id": str(c["id"]),
                        "name": str(c["name"]).lstrip("#"),
                        "archived": bool(c.get("is_archived", False)),
                        "private": bool(c.get("is_private", False)),
                    }
                )
    else:
        for block in re.split(r"\n### Result", text):
            m_name = re.search(r"Name:\s*#?([\w.-]+)", block)
            m_link = re.search(r"/archives/([CG][A-Z0-9]{6,})", block)
            if m_name and m_link:
                found.append(
                    {
                        "id": m_link.group(1),
                        "name": m_name.group(1),
                        "archived": bool(re.search(r"Is Archived:\s*true", block, re.I)),
                        "private": "private_channel" in block,
                    }
                )
    exact = [c for c in found if c["name"] == name and not c["archived"]]
    if not exact:
        raise NotFoundError(
            f"Slack channel #{name} was not found (or is archived); it is never created",
            details={"seen": [c["name"] for c in found]},
        )
    if len({c["id"] for c in exact}) > 1:
        raise AmbiguousRequestError(f"several channels are named #{name}", details={"ids": exact})
    return {**exact[0], "verified_at": _now(), "source": "slack_search_channels"}


def set_policy(
    *,
    channel: dict[str, Any] | None,
    enable: bool,
    events: list[str] | None,
    approve: bool,
) -> dict[str, Any]:
    if enable and not approve:
        raise ApprovalRequiredError(
            "Enabling automatic Slack notifications needs --approve (organisation-approved "
            "policy, confirmed by the engineer once)"
        )
    if enable and channel is None:
        raise InvalidInputError("enable needs --channel-from <slack_search_channels result>")
    chosen = events or DEFAULT_EVENTS
    unknown = [e for e in chosen if e not in EVENTS]
    if unknown:
        raise InvalidInputError(f"unknown events {unknown}", details={"events": list(EVENTS)})
    policy = (
        {
            "enabled": True,
            "channel": channel,
            "events": chosen,
            "approved_at": _now(),
        }
        if enable
        else {**load_policy(), "enabled": False, "disabled_at": _now()}
    )
    _write_atomic(policy_path(), policy)
    return policy


# ------------------------------------------------------------------------------ message
def _line(label: str, value: Any) -> str | None:
    if value in (None, "", [], {}):
        return None
    if isinstance(value, list):
        value = ", ".join(str(v) for v in value)
    return f"• *{label}:* {value}"


def build_message(op: dict[str, Any], event: str) -> str:
    """Concise structured Markdown (what the Slack MCP accepts). Facts come from the
    operation's recorded engine evidence only; everything is redacted."""
    entry = next((e for e in reversed(op["events"]) if e["event"] == event), None)
    facts: dict[str, Any] = dict(entry["facts"]) if entry else {}
    actor = op["actor"]
    who = actor.get("display", "unverified")
    if actor.get("slack_user_id"):
        who = f"<@{actor['slack_user_id']}>"
    if not actor.get("verified"):
        who = "unverified engineer"
    phase = PHASE[event].upper()
    approvals = op["approvals"] if event == "APPROVAL_GRANTED" else []
    approved = approvals[-1]["step"] if approvals else None
    lines = [
        f"*Library release · {event.replace('_', ' ').title()}* — `{phase}`",
        _line("Operation", f"`{op['id']}` ({op['kind']})"),
        _line("Engineer", who),
        _line("Repository", op.get("repository")),
        _line("Library", op["library"]),
        _line("Requested order", facts.get("requested_order") or op["request"].get("order")),
        _line("Recommended order", facts.get("recommended_order")),
        _line("Tickets", op["request"].get("tickets")),
        _line("New candidates", facts.get("new_candidates")),
        _line("Expected versions", facts.get("expected_versions")),
        _line("Release steps", _steps(facts.get("release_steps") or [])),
        _line("Version", facts.get("version") or facts.get("tag")),
        _line("Readiness", facts.get("readiness")),
        _line("Affected services", facts.get("affected_services")),
        _line("Blocked because", facts.get("blocked_evidence") or facts.get("blockers")),
        _line("Status", facts.get("status")),
        _line("Reason", facts.get("reason")),
        _line("Link", facts.get("html_url") or facts.get("url")),
        _line("Approved step", approved),
        f"_{PHASE[event]} · {entry['at'] if entry else _now()} · nothing here is an approval_",
        f"`lro:{op['id']}:{event}`",
    ]
    text = "\n".join(x for x in lines if x)
    return str(redact(text))[:3900]


def _steps(steps: list[dict[str, Any]]) -> str:
    out = []
    for n, s in enumerate(steps, 1):
        via = (s.get("new_candidate") and f"new {s['new_candidate']}") or s.get("candidate")
        out.append(f"{n}) {', '.join(s.get('tickets') or [])} -> {s.get('version')} via {via}")
    return "; ".join(out)


# ------------------------------------------------------------------------------ outbox
class Notifier:
    def __init__(self, store: OperationStore) -> None:
        self.store = store

    def _lock(self, op_id: str) -> _FileLock:
        return _FileLock(self.store.root / f".{op_id}.lock")

    def plan(self, op_id: str, event: str) -> dict[str, Any]:
        policy = load_policy()
        if not policy.get("enabled"):
            return {"action": "disabled", "reason": "Slack notifications are not enabled"}
        if event not in policy.get("events", []):
            return {"action": "skip", "reason": f"{event} is not in the notification policy"}
        channel = policy["channel"]["id"]
        with self._lock(op_id):
            op = self.store.load(op_id)
            if not any(e["event"] == event for e in op["events"]):
                raise InvalidInputError(
                    f"{event} is not recorded on {op_id}; record it with engine evidence first"
                )
            notes = op["notifications"]
            deliveries = notes["deliveries"]
            current = deliveries.get(event, {})
            if current.get("state") == "delivered":
                return {"action": "skip", "reason": "already delivered", "ts": current.get("ts")}
            pending = [ev for ev, d in deliveries.items() if d["state"] in ("pending", "uncertain")]
            if pending:
                return {
                    "action": "reconcile",
                    "events": pending,
                    "channel_id": channel,
                    "thread_ts": notes.get("thread_ts"),
                    "marker": [f"lro:{op_id}:{ev}" for ev in pending],
                    "instruction": "read the thread (or channel) history and run notify reconcile; "
                    "never re-post an uncertain message",
                }
            thread_ts = notes.get("thread_ts")
            deliveries[event] = {
                "state": "pending",
                "attempts": current.get("attempts", 0) + 1,
                "planned_at": _now(),
                "parent": thread_ts is None,
            }
            notes["channel_id"] = channel
            self.store.save(op)
            return {
                "action": "post",
                "channel_id": channel,
                "thread_ts": thread_ts,
                "message": build_message(op, event),
                "marker": f"lro:{op_id}:{event}",
            }

    def record(self, op_id: str, event: str, response: str) -> dict[str, Any]:
        """Record the Slack MCP response (or error text) for a pending delivery."""
        with self._lock(op_id):
            op = self.store.load(op_id)
            notes = op["notifications"]
            current = notes["deliveries"].get(event)
            if not current or current["state"] not in ("pending", "uncertain"):
                raise InvalidInputError(f"no pending Slack delivery for {event} on {op_id}")
            ts, channel = _ts_and_channel(response)
            lowered = response.lower()
            if ts and (channel in (None, notes.get("channel_id"))):
                current.update(state="delivered", ts=ts, delivered_at=_now())
                if current.get("parent") and not notes.get("thread_ts"):
                    notes["thread_ts"] = ts
            elif any(code in lowered for code in DEFINITE_FAILURES):
                current.update(state="not_sent", error=str(redact(response[:300])))
            else:
                current.update(state="uncertain", error=str(redact(response[:300])))
            self.store.save(op)
            return {"event": event, **current, "thread_ts": notes.get("thread_ts")}

    def reconcile(self, op_id: str, history: str) -> dict[str, Any]:
        """Resolve pending/uncertain deliveries from channel or thread history."""
        with self._lock(op_id):
            op = self.store.load(op_id)
            notes = op["notifications"]
            resolved = {}
            for event, d in notes["deliveries"].items():
                if d["state"] not in ("pending", "uncertain"):
                    continue
                ts = _find_marker(history, f"lro:{op_id}:{event}")
                if ts is False:
                    d.update(state="not_sent", reconciled_at=_now())
                else:
                    d.update(state="delivered", ts=ts or d.get("ts"), reconciled_at=_now())
                    if d.get("parent") and not notes.get("thread_ts") and ts:
                        notes["thread_ts"] = ts
                resolved[event] = d["state"]
            self.store.save(op)
            return {"resolved": resolved, "thread_ts": notes.get("thread_ts")}


def _ts_and_channel(response: str) -> tuple[str | None, str | None]:
    try:
        data = json.loads(response)
    except ValueError:
        data = None
    if isinstance(data, dict):
        if data.get("ok") is False or data.get("error"):
            return None, None
        ts = data.get("ts") or (data.get("message") or {}).get("ts")
        if isinstance(ts, str) and _TS.fullmatch(ts):
            return ts, data.get("channel")
        response = json.dumps(data)
    m = _PERMALINK.search(response)
    if m:
        return f"{m.group(2)}.{m.group(3)}", m.group(1)
    return None, None


def _find_marker(history: str, marker: str) -> str | bool | None:
    """ts of the message carrying ``marker``; None if found without a ts; False if absent."""
    try:
        data = json.loads(history)
    except ValueError:
        data = None
    if isinstance(data, dict):
        for msg in data.get("messages", []):
            if isinstance(msg, dict) and marker in str(msg.get("text", "")):
                return str(msg.get("ts")) if msg.get("ts") else None
        history = json.dumps(data)
    idx = history.find(marker)
    if idx < 0:
        return False
    window = history[max(0, idx - 4000) : idx + 400]
    links = list(_PERMALINK.finditer(window))
    if links:
        m = links[-1]
        return f"{m.group(2)}.{m.group(3)}"
    stamps = _TS.findall(window)
    return stamps[-1] if stamps else None
