"""Release operations: correlation IDs, engine-backed events, identity and audit (ADR-0018).

An *operation* is one engineer-initiated release activity (create an RC, release a
candidate, reorder candidates). It has a stable ID that every event, approval, Slack
notification and audit line carries.

Events are facts, not narration: an event that claims an outcome (plan ready, RC created,
release completed, validation failed, blocked) must cite the engine's own JSON output, and
the engine checks that the output supports the claim. Claude can't record "release
completed" for a publish that didn't happen.

The audit report is a local JSON/Markdown file. It is an honest record of what the engine
saw and did; it is **not** a tamper-proof compliance system.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import BlockedError, InvalidInputError, NotFoundError

KINDS = ("create-rc", "release", "reorder")
EVENTS = (
    "REORDER_REQUESTED",
    "REORDER_BLOCKED",
    "PLAN_READY",
    "VALIDATION_STARTED",
    "VALIDATION_FAILED",
    "APPROVAL_GRANTED",
    "RC_CREATED",
    "PR_CREATED",
    "RELEASE_COMPLETED",
    "OPERATION_FAILED",
    "OPERATION_CANCELLED",
)
TERMINAL = {"RELEASE_COMPLETED", "REORDER_BLOCKED", "OPERATION_FAILED", "OPERATION_CANCELLED"}
PHASE = {
    "REORDER_REQUESTED": "planned",
    "PLAN_READY": "planned",
    "REORDER_BLOCKED": "blocked",
    "APPROVAL_GRANTED": "approved",
    "VALIDATION_STARTED": "executing",
    "RC_CREATED": "executed",
    "PR_CREATED": "executed",
    "RELEASE_COMPLETED": "executed",
    "VALIDATION_FAILED": "failed",
    "OPERATION_FAILED": "failed",
    "OPERATION_CANCELLED": "cancelled",
}
ACTIVE_TTL_SECONDS = 24 * 3600

# ------------------------------------------------------------------------------ redaction
_SECRET_PATTERNS = [
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"xox[abposr]-[A-Za-z0-9-]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"https://hooks\.slack\.com/[^\s)>\]]+"),
    re.compile(
        r"(?i)\b(password|passwd|secret|token|api[_-]?key|access[_-]?key)\b(\s*[:=]\s*)[^\s,;]+"
    ),
    re.compile(r"(?i)(https?://)[^\s/:@]+:[^\s/@]+@"),  # credentials in URLs
]


def redact(value: Any) -> Any:
    """Recursively replace secret-looking substrings with ``[REDACTED]``."""
    if isinstance(value, str):
        out = value
        for pattern in _SECRET_PATTERNS:
            if pattern.groups >= 2:
                out = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", out)
            elif pattern.groups == 1:
                out = pattern.sub(lambda m: f"{m.group(1)}[REDACTED]@", out)
            else:
                out = pattern.sub("[REDACTED]", out)
        return out
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact(v) for v in value]
    return value


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _write_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


class _FileLock:
    def __init__(self, path: Path, attempts: int = 250) -> None:
        self.path, self.attempts = path, attempts

    def __enter__(self) -> _FileLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(self.attempts):
            try:
                os.close(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                return self
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > 120:
                        self.path.unlink(missing_ok=True)
                        continue
                except FileNotFoundError:
                    continue
                time.sleep(0.02)
        raise BlockedError(
            f"operation store is locked ({self.path.name})", reason="OPERATION_LOCKED"
        )

    def __exit__(self, *_: object) -> None:
        self.path.unlink(missing_ok=True)


# ------------------------------------------------------------------------------ identity
def parse_actor(payload: str, source: str) -> dict[str, Any]:
    """Identity from an authenticated MCP read (Slack profile or GitHub ``get_me``).

    Only fields present in the payload are used; nothing is guessed. A Slack mention is
    produced only from a Slack user ID found in the payload.
    """
    text = payload.strip()
    actor: dict[str, Any] = {"verified": False, "source": source}
    data: Any = None
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, dict) and isinstance(data.get("result"), str):
        text, data = data["result"], None
    if isinstance(data, dict):
        nested = data.get("user")
        user: dict[str, Any] = nested if isinstance(nested, dict) else data
        prof = user.get("profile")
        profile: dict[str, Any] = prof if isinstance(prof, dict) else {}
        login = user.get("login")
        uid = user.get("id") if isinstance(user.get("id"), str) else None
        name = user.get("real_name") or profile.get("real_name") or user.get("name")
        if login:
            actor.update(github_login=str(login), display=str(name or login), verified=True)
        elif uid and re.fullmatch(r"[UW][A-Z0-9]{6,}", uid):
            actor.update(slack_user_id=uid, display=str(name or uid), verified=True)
        elif name:
            actor.update(display=str(name), verified=True)
    else:
        first = text.splitlines()[0].strip() if text else ""
        m = re.match(r"^([\w.\-]+)\s*\(([^)]+)\)$", first)
        if m:
            actor.update(handle=m.group(1), display=m.group(2).strip(), verified=True)
        uid = re.search(r"(?i)user id:\s*([UW][A-Z0-9]{6,})", text)
        if uid:
            actor["slack_user_id"] = uid.group(1)
    if not actor["verified"]:
        actor["display"] = "unverified"
    return actor


# ------------------------------------------------------------------------------ evidence
def _envelope(raw: Any) -> tuple[bool, dict[str, Any]]:
    """(ok, result) from an engine envelope ``{ok, result|error}`` or a raw result file."""
    if isinstance(raw, dict) and "ok" in raw and ("result" in raw or "error" in raw):
        body = raw.get("result") if raw["ok"] else raw.get("error")
        return bool(raw["ok"]), body if isinstance(body, dict) else {}
    return isinstance(raw, dict) and raw.get("readiness") != "BLOCKED", (
        raw if isinstance(raw, dict) else {}
    )


def summarise(raw: Any) -> dict[str, Any]:
    """The facts a notification or audit line may show, taken from engine output only."""
    ok, body = _envelope(raw)
    out: dict[str, Any] = {"engine_ok": ok}
    if "options" in body:  # release-mode options
        opts = body["options"]
        req = next((o for o in opts if o.get("id") == "requested"), None)
        rec = next((o for o in opts if o.get("recommended")), None)
        if req:
            out["requested_order"] = req.get("label")
            out["requested_valid"] = req.get("valid")
        if rec:
            out["recommended_order"] = rec.get("label")
            out["new_candidates"] = [
                p.get("new_candidate") for p in rec.get("produces", []) if p.get("new_candidate")
            ]
            out["expected_versions"] = [p.get("version") for p in rec.get("produces", [])]
        if req and not req.get("valid"):
            out["blocked_evidence"] = list(req.get("evidence", []))[:5]
    if "readiness" in body:  # release-mode preview
        out["readiness"] = body["readiness"]
        out["affected_services"] = body.get("affected_services", [])
        out["blockers"] = list(body.get("blockers", []))[:5]
        out["release_steps"] = [
            {k: s.get(k) for k in ("candidate", "tickets", "version", "new_candidate", "status")}
            for s in body.get("release_steps", [])
        ]
        out["preview_id"] = body.get("preview_id")
    for key in ("status", "reason", "version", "tag", "plan_id", "url", "html_url", "message"):
        if isinstance(body.get(key), str | int):
            out[key] = body[key]
    for key in ("validate", "publish", "release"):
        if isinstance(body.get(key), dict):
            sub = body[key]
            out[key] = {k: sub[k] for k in ("status", "version", "tag") if k in sub}
    return redact(out)  # type: ignore[no-any-return]


def _supports(event: str, ok: bool, facts: dict[str, Any]) -> str | None:
    """Return why the engine output does *not* support the event, or None."""
    status = str(facts.get("status", "")).lower()
    if event == "PLAN_READY" and not ok:
        return "the cited engine output is not ok"
    if event == "PLAN_READY" and facts.get("readiness") == "BLOCKED":
        return "the preview is BLOCKED"
    if event in ("RC_CREATED", "RELEASE_COMPLETED"):
        published = status in ("published", "already_published") or facts.get("publish", {}).get(
            "status"
        ) in ("published", "already_published")
        if not (ok and published):
            return "the cited engine output does not show a published version"
    if event == "PR_CREATED" and not (ok and (facts.get("url") or facts.get("html_url"))):
        return "the cited engine output has no recorded PR URL"
    if event == "VALIDATION_FAILED" and ok and "fail" not in status:
        return "the cited engine output does not show a failed validation"
    if event == "REORDER_BLOCKED" and not (
        not ok or facts.get("requested_valid") is False or facts.get("readiness") == "BLOCKED"
    ):
        return "the cited engine output is not blocked"
    return None


NEEDS_EVIDENCE = {
    "PLAN_READY",
    "REORDER_BLOCKED",
    "RC_CREATED",
    "PR_CREATED",
    "RELEASE_COMPLETED",
    "VALIDATION_FAILED",
}


# ------------------------------------------------------------------------------ store
@dataclass
class OperationStore:
    state_dir: Path

    @property
    def root(self) -> Path:
        return self.state_dir / "operations"

    def _path(self, op_id: str) -> Path:
        if not re.fullmatch(r"op-\d{8}T\d{6}Z-[0-9a-f]{8}", op_id):
            raise InvalidInputError(f"not an operation id: {op_id!r}")
        return self.root / f"{op_id}.json"

    def load(self, op_id: str) -> dict[str, Any]:
        path = self._path(op_id)
        if not path.is_file():
            raise NotFoundError(f"unknown operation {op_id}")
        return dict(json.loads(path.read_text(encoding="utf-8")))

    def save(self, op: dict[str, Any]) -> None:
        op["updated_at"] = _now()
        _write_atomic(self._path(op["id"]), redact(op))

    def active(self, library: str) -> list[dict[str, Any]]:
        out = []
        for path in sorted(self.root.glob("op-*.json")):
            try:
                op = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if op.get("library") != library or op.get("terminal"):
                continue
            created = time.mktime(time.strptime(op["created_at"], "%Y-%m-%dT%H:%M:%SZ"))
            if time.mktime(time.gmtime()) - created < ACTIVE_TTL_SECONDS:
                out.append(op)
        return out

    def start(
        self,
        *,
        kind: str,
        library: str,
        repository: str | None,
        request: dict[str, Any],
        engine: dict[str, Any],
        allow_concurrent: bool = False,
    ) -> dict[str, Any]:
        if kind not in KINDS:
            raise InvalidInputError(f"kind must be one of {KINDS}")
        with _FileLock(self.root / ".start.lock"):
            active = self.active(library)
            if active and not allow_concurrent:
                raise BlockedError(
                    f"another release operation for {library} is active: {active[0]['id']}",
                    reason="CONCURRENT_OPERATION",
                    details={
                        "active": [
                            {"id": o["id"], "kind": o["kind"], "actor": o["actor"].get("display")}
                            for o in active
                        ],
                        "remediation": "finish it, or record OPERATION_CANCELLED on it",
                    },
                )
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            op = {
                "id": f"op-{stamp}-{secrets.token_hex(4)}",
                "kind": kind,
                "library": library,
                "repository": repository,
                "request": redact(request),
                "engine": engine,
                "actor": {"verified": False, "display": "unverified", "source": None},
                "created_at": _now(),
                "events": [],
                "approvals": [],
                "notifications": {"thread_ts": None, "channel_id": None, "deliveries": {}},
                "warnings": [],
                "terminal": None,
            }
            self.save(op)
            return op

    def set_actor(self, op_id: str, payload: str, source: str) -> dict[str, Any]:
        with _FileLock(self.root / f".{op_id}.lock"):
            op = self.load(op_id)
            op["actor"] = parse_actor(payload, source)
            self.save(op)
            return op

    def record(
        self,
        op_id: str,
        event: str,
        *,
        evidence: Path | None = None,
        note: str = "",
        approved_step: str | None = None,
    ) -> dict[str, Any]:
        if event not in EVENTS:
            raise InvalidInputError(f"unknown event {event!r}", details={"events": list(EVENTS)})
        facts: dict[str, Any] = {}
        digest = None
        if evidence is not None:
            raw_text = evidence.read_text(encoding="utf-8")
            digest = hashlib.sha256(raw_text.encode()).hexdigest()
            raw = json.loads(raw_text)
            facts = summarise(raw)
            why = _supports(event, bool(facts.get("engine_ok")), facts)
            if why:
                raise BlockedError(
                    f"{event} is not supported by the engine output: {why}",
                    reason="EVENT_NOT_SUPPORTED_BY_EVIDENCE",
                    details={"facts": facts},
                )
        elif event in NEEDS_EVIDENCE:
            raise InvalidInputError(
                f"{event} must cite the engine output that shows it (--evidence FILE)"
            )
        if event == "APPROVAL_GRANTED" and not approved_step:
            raise InvalidInputError("APPROVAL_GRANTED needs --step (what was approved)")
        with _FileLock(self.root / f".{op_id}.lock"):
            op = self.load(op_id)
            if op.get("terminal") and event != "OPERATION_CANCELLED":
                raise BlockedError(
                    f"operation {op_id} already ended with {op['terminal']}",
                    reason="OPERATION_ENDED",
                )
            entry = {
                "event": event,
                "phase": PHASE[event],
                "at": _now(),
                "facts": facts,
                "evidence_sha256": digest,
                "note": redact(note[:500]) if note else "",
            }
            op["events"].append(entry)
            if event == "APPROVAL_GRANTED":
                op["approvals"].append(
                    {"step": approved_step, "at": entry["at"], "by": op["actor"].get("display")}
                )
            if event in TERMINAL and not op.get("terminal"):
                op["terminal"] = event
            self.save(op)
            return op

    def report(self, op_id: str) -> dict[str, Any]:
        op = self.load(op_id)
        deliveries = op["notifications"]["deliveries"]
        return {
            "operation": op["id"],
            "kind": op["kind"],
            "status": op.get("terminal") or "in progress",
            "actor": op["actor"],
            "repository": op.get("repository"),
            "library": op["library"],
            "engine": op["engine"],
            "request": op["request"],
            "timeline": [
                {k: e[k] for k in ("at", "event", "phase", "facts", "evidence_sha256", "note")}
                for e in op["events"]
            ],
            "approvals": op["approvals"],
            "notifications": {
                "channel_id": op["notifications"].get("channel_id"),
                "thread_ts": op["notifications"].get("thread_ts"),
                "deliveries": deliveries,
            },
            "warnings": op["warnings"],
            "limitations": [
                "Local audit file: an honest record of engine results, not a tamper-proof "
                "compliance system.",
                "Identity is as verified as its source; 'unverified' means no authenticated "
                "identity evidence was provided.",
            ],
        }


def render_report(rep: dict[str, Any]) -> str:
    actor = rep["actor"]
    who = actor.get("display", "unverified")
    who += f" (verified via {actor['source']})" if actor.get("verified") else " (unverified)"
    lines = [
        f"# Release operation {rep['operation']}",
        "",
        f"* Kind: {rep['kind']} · Status: **{rep['status']}**",
        f"* Engineer: {who}",
        f"* Repository: {rep.get('repository') or 'unknown'} · Library: {rep['library']}",
        f"* Engine: {rep['engine'].get('revision', 'unknown')}",
        f"* Request: `{json.dumps(rep['request'], sort_keys=True)}`",
        "",
        "## Timeline",
        "",
        "| Time (UTC) | Event | Phase | Engine facts |",
        "|---|---|---|---|",
    ]
    for e in rep["timeline"]:
        facts = ", ".join(f"{k}={v}" for k, v in e["facts"].items() if k != "engine_ok")
        lines.append(f"| {e['at']} | {e['event']} | {e['phase']} | {facts or e['note'] or '-'} |")
    lines += ["", "## Approvals", ""]
    lines += [f"* {a['at']}: {a['step']} (by {a['by']})" for a in rep["approvals"]] or ["* none"]
    lines += ["", "## Slack notifications", ""]
    deliveries = rep["notifications"]["deliveries"]
    lines += [
        f"* {ev}: {d['state']} (attempts {d['attempts']}, ts {d.get('ts') or '-'})"
        for ev, d in deliveries.items()
    ] or ["* none"]
    lines += ["", "## Limitations", ""] + [f"* {x}" for x in rep["limitations"]]
    return "\n".join(lines) + "\n"
