"""ADR-0018 acceptance O-S, V, W, Y: operations, engine-backed events, Slack outbox.

Mocked: every Slack MCP payload. The search-result shape mirrors the real
``slack_search_channels`` Markdown output (captured read-only); IDs and names here are
synthetic. No message is sent anywhere.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from release_orchestrator.errors import (
    AmbiguousRequestError,
    ApprovalRequiredError,
    BlockedError,
    InvalidInputError,
    NotFoundError,
)
from release_orchestrator.notify import Notifier, build_message, resolve_channel, set_policy
from release_orchestrator.operations import OperationStore, parse_actor, redact

CHANNEL_ID = "C0TEST00001"
SEARCH_MD = json.dumps(
    {
        "results": (
            "# Search Results for: \n\n## Channels (2 results)\n### Result 1 of 2\n"
            "Name: #library-release-orchestrator\nCreator: Someone (<@U0TEST0001)\n"
            "Purpose: release notifications\n"
            f"Permalink: [link](https://example.slack.com/archives/{CHANNEL_ID})\n"
            "Is Archived: false\nChannel Type: private_channel\n\n---\n"
            "### Result 2 of 2\nName: #library-release-orchestrator-old\n"
            "Permalink: [link](https://example.slack.com/archives/C0TEST00002)\n"
            "Is Archived: false\n\n---\n"
        ),
        "pagination_info": "End of results",
    }
)
SENT = "Message sent: https://example.slack.com/archives/" + CHANNEL_ID + "/p1764000000123456"


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[OperationStore, Notifier]:
    monkeypatch.setenv("LRO_HOME", str(tmp_path / "home"))
    store = OperationStore(tmp_path / "state")
    return store, Notifier(store)


def _start(store: OperationStore, **kw: object) -> dict:
    return store.start(
        kind="reorder",
        library="toolkit",
        repository="acme/toolkit",
        request={"order": ["v1.1.0-rc.2", "v1.1.0-rc.1"], "tickets": []},
        engine={"version": "0.1.0", "revision": "abc (pinned install)"},
        **kw,  # type: ignore[arg-type]
    )


def _enable() -> None:
    set_policy(channel=resolve_channel(SEARCH_MD), enable=True, events=None, approve=True)


def _evidence(tmp_path: Path, name: str, data: dict) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- O
def test_o_channel_is_resolved_exactly_and_never_guessed():
    ch = resolve_channel(SEARCH_MD)
    assert (ch["id"], ch["name"], ch["private"]) == (
        CHANNEL_ID,
        "library-release-orchestrator",
        True,
    )
    js = json.dumps({"channels": [{"id": "C0TEST00009", "name": "library-release-orchestrator"}]})
    assert resolve_channel(js)["id"] == "C0TEST00009"
    archived = SEARCH_MD.replace(
        "Is Archived: false\\nChannel Type", "Is Archived: true\\nChannel Type"
    )
    with pytest.raises(NotFoundError):
        resolve_channel(archived)
    with pytest.raises(NotFoundError):
        resolve_channel(json.dumps({"results": "## Channels (0 results)"}))
    twice = json.dumps(
        {
            "channels": [
                {"id": "C0TEST00009", "name": "library-release-orchestrator"},
                {"id": "C0TEST00010", "name": "library-release-orchestrator"},
            ]
        }
    )
    with pytest.raises(AmbiguousRequestError):
        resolve_channel(twice)


def test_policy_needs_explicit_approval_and_disabled_means_silent(env):
    store, notifier = env
    with pytest.raises(ApprovalRequiredError):
        set_policy(channel=resolve_channel(SEARCH_MD), enable=True, events=None, approve=False)
    op = _start(store)
    store.record(op["id"], "REORDER_REQUESTED")
    assert notifier.plan(op["id"], "REORDER_REQUESTED")["action"] == "disabled"


# --------------------------------------------------------------------------- P, Q
def test_p_q_parent_then_thread_replies_without_duplicates(env, tmp_path):
    store, notifier = env
    _enable()
    op = _start(store)
    store.record(op["id"], "REORDER_REQUESTED")
    first = notifier.plan(op["id"], "REORDER_REQUESTED")
    assert first["action"] == "post" and first["thread_ts"] is None
    assert first["channel_id"] == CHANNEL_ID
    assert f"lro:{op['id']}:REORDER_REQUESTED" in first["message"]
    # Q: planned but not recorded -> never re-posted blindly
    assert notifier.plan(op["id"], "REORDER_REQUESTED")["action"] == "reconcile"
    rec = notifier.record(op["id"], "REORDER_REQUESTED", SENT)
    assert rec["state"] == "delivered" and rec["thread_ts"] == "1764000000.123456"
    assert notifier.plan(op["id"], "REORDER_REQUESTED")["action"] == "skip"
    options = {
        "ok": True,
        "result": {
            "options": [
                {
                    "id": "requested",
                    "label": "v1.1.0-rc.2 -> v1.1.0-rc.1",
                    "valid": True,
                    "recommended": True,
                    "produces": [
                        {"new_candidate": "v1.1.0-rc.3", "version": "1.1.0"},
                        {"new_candidate": "v1.2.0-rc.1", "version": "1.2.0"},
                    ],
                }
            ]
        },
    }
    preview = {"readiness": "READY", "affected_services": ["payment"], "release_steps": []}
    store.record(op["id"], "PLAN_READY", evidence=_evidence(tmp_path, "pv.json", preview))
    reply = notifier.plan(op["id"], "PLAN_READY")
    assert reply["action"] == "post" and reply["thread_ts"] == "1764000000.123456"
    assert "payment" in reply["message"] and "READY" in reply["message"]
    store.record(op["id"], "APPROVAL_GRANTED", approved_step="prepare step 1", note="yes")
    assert "Approval Granted" in build_message(store.load(op["id"]), "APPROVAL_GRANTED")
    _ = options


def test_q_uncertain_delivery_is_reconciled_from_history(env):
    store, notifier = env
    _enable()
    op = _start(store)
    store.record(op["id"], "REORDER_REQUESTED")
    notifier.plan(op["id"], "REORDER_REQUESTED")
    assert notifier.record(op["id"], "REORDER_REQUESTED", "timeout after 30s")["state"] == (
        "uncertain"
    )
    marker = f"lro:{op['id']}:REORDER_REQUESTED"
    history = json.dumps({"messages": [{"ts": "1764000001.000001", "text": f"x\n`{marker}`"}]})
    out = notifier.reconcile(op["id"], history)
    assert out["resolved"] == {"REORDER_REQUESTED": "delivered"}
    assert out["thread_ts"] == "1764000001.000001"
    assert notifier.plan(op["id"], "REORDER_REQUESTED")["action"] == "skip"  # no duplicate


# --------------------------------------------------------------------------- R
def test_r_failed_delivery_never_blocks_the_release_and_can_retry(env, tmp_path):
    store, notifier = env
    _enable()
    op = _start(store)
    store.record(op["id"], "REORDER_REQUESTED")
    notifier.plan(op["id"], "REORDER_REQUESTED")
    assert (
        notifier.record(op["id"], "REORDER_REQUESTED", '{"ok": false, "error": "not_in_channel"}')[
            "state"
        ]
        == "not_sent"
    )
    # the release goes on: events keep being recorded
    published = {"ok": True, "result": {"status": "published", "version": "1.1.0-rc.3"}}
    store.record(op["id"], "RC_CREATED", evidence=_evidence(tmp_path, "pub.json", published))
    again = notifier.plan(op["id"], "REORDER_REQUESTED")
    assert again["action"] == "post"
    assert store.load(op["id"])["notifications"]["deliveries"]["REORDER_REQUESTED"]["attempts"] == 2
    # absent from history after an uncertain attempt -> not sent, safe to retry
    notifier.record(op["id"], "REORDER_REQUESTED", "connection reset")
    assert notifier.reconcile(op["id"], json.dumps({"messages": []}))["resolved"] == {
        "REORDER_REQUESTED": "not_sent"
    }


# --------------------------------------------------------------------------- S
def test_s_identity_is_verified_only_from_authenticated_payloads(env):
    store, _ = env
    op = _start(store)
    assert op["actor"]["display"] == "unverified"
    assert "unverified engineer" in build_message(
        {**op, "events": [{"event": "REORDER_REQUESTED", "facts": {}, "at": "t"}]},
        "REORDER_REQUESTED",
    )
    slack_text = json.dumps({"result": "anas.dev (Anas Example)\nAcme\nEngineer\n"})
    actor = parse_actor(slack_text, "slack_read_user_profile")
    assert actor == {
        "verified": True,
        "source": "slack_read_user_profile",
        "handle": "anas.dev",
        "display": "Anas Example",
    }
    assert "slack_user_id" not in actor  # never a guessed mention
    gh = parse_actor(json.dumps({"login": "anas-example", "name": "Anas"}), "get_me")
    assert (gh["github_login"], gh["verified"]) == ("anas-example", True)
    slack_json = parse_actor(
        json.dumps({"user": {"id": "U0TEST0042", "real_name": "Anas"}}), "slack_read_user_profile"
    )
    assert slack_json["slack_user_id"] == "U0TEST0042"
    assert parse_actor("garbage", "x")["verified"] is False
    updated = store.set_actor(op["id"], slack_text, "slack_read_user_profile")
    assert updated["actor"]["display"] == "Anas Example"


# --------------------------------------------------------------------------- V: evidence
def test_v_outcomes_cannot_be_claimed_without_engine_evidence(env, tmp_path):
    store, _ = env
    op = _start(store)
    with pytest.raises(InvalidInputError):
        store.record(op["id"], "RELEASE_COMPLETED")  # no evidence
    not_published = {"ok": True, "result": {"status": "prepared"}}
    with pytest.raises(BlockedError) as exc:
        store.record(
            op["id"], "RELEASE_COMPLETED", evidence=_evidence(tmp_path, "a.json", not_published)
        )
    assert exc.value.reason == "EVENT_NOT_SUPPORTED_BY_EVIDENCE"
    blocked_preview = {"readiness": "BLOCKED", "blockers": ["FIN-198 missing"]}
    with pytest.raises(BlockedError):
        store.record(
            op["id"], "PLAN_READY", evidence=_evidence(tmp_path, "b.json", blocked_preview)
        )
    store.record(
        op["id"], "REORDER_BLOCKED", evidence=_evidence(tmp_path, "c.json", blocked_preview)
    )
    with pytest.raises(BlockedError) as exc:
        store.record(op["id"], "VALIDATION_STARTED")  # the operation already ended
    assert exc.value.reason == "OPERATION_ENDED"
    with pytest.raises(InvalidInputError):
        store.record(op["id"], "APPROVAL_GRANTED")  # what was approved?


# --------------------------------------------------------------------------- W
def test_w_concurrent_operations_are_refused_and_writes_are_serialised(env):
    store, _ = env
    op = _start(store)
    with pytest.raises(BlockedError) as exc:
        _start(store)
    assert exc.value.reason == "CONCURRENT_OPERATION"
    errors: list[BaseException] = []

    def note(n: int) -> None:
        try:
            store.record(op["id"], "VALIDATION_STARTED", note=f"run {n}")
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=note, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(store.load(op["id"])["events"]) == 8  # no lost update
    store.record(op["id"], "OPERATION_CANCELLED")
    assert _start(store)["id"] != op["id"]  # the library is free again


# --------------------------------------------------------------------------- Y
def test_y_no_secrets_in_audit_or_slack(env, tmp_path):
    store, notifier = env
    _enable()
    secrets = [
        "ghp_" + "a" * 36,
        "xoxb-" + "1" * 12 + "-abcdefghijkl",
        "AKIA" + "B" * 16,
        "https://user:hunter2pass@example.com/repo.git",
    ]
    op = store.start(
        kind="reorder",
        library="toolkit",
        repository="acme/toolkit",
        request={"order": [secrets[0]], "tickets": ["password=letmein"]},
        engine={"revision": "x"},
    )
    store.record(op["id"], "REORDER_REQUESTED", note=" ".join(secrets))
    preview = {"readiness": "READY", "blockers": [f"token: {secrets[1]}"], "affected_services": []}
    store.record(op["id"], "PLAN_READY", evidence=_evidence(tmp_path, "p.json", preview))
    raw = (store.root / f"{op['id']}.json").read_text(encoding="utf-8")
    message = notifier.plan(op["id"], "REORDER_REQUESTED")["message"]
    report = json.dumps(store.report(op["id"]))
    for blob in (raw, message, report):
        for s in [*secrets, "letmein", "hunter2pass"]:
            assert s not in blob
    assert "[REDACTED]" in raw
    assert redact({"k": ["Bearer abcdefghijklmnop"]}) == {"k": ["[REDACTED]"]}
