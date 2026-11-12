"""Authoritative PR status and the approval policy (ADR-0011)."""

from __future__ import annotations

import pytest

from release_orchestrator.errors import IntegrationError
from release_orchestrator.integrations.github_status import (
    ApprovalPolicy,
    CheckRun,
    GitHubAccessError,
    GitHubReader,
    PullEvidence,
    PullStatus,
    Review,
    evaluate_approval,
)

HEAD = "a" * 40
OLD = "b" * 40


def evidence(  # type: ignore[no-untyped-def]
    *,
    reviews=(),
    checks=(),
    draft=False,
    state="open",
    merged=False,
    head=HEAD,
    permissions=None,
    statuses=None,
    protection=None,
    protection_note="unverified (404)",
):
    pull = PullStatus(
        "o/r",
        7,
        state,
        draft,
        merged,
        "release/1.1.0",
        head,
        "release-base/v1.0.0",
        "author",
        "https://github.com/o/r/pull/7",
    )
    perms = {r.user: "write" for r in reviews} if permissions is None else permissions
    return PullEvidence(
        pull,
        list(reviews),
        list(checks),
        "2026-11-10T00:00:00Z",
        statuses or {},
        perms,
        protection,
        protection_note,
    )


POLICY = ApprovalPolicy(required_approvals=1, required_checks=("ci",))
GREEN = [CheckRun("ci", "completed", "success", "2026-11-10T10:00:00Z")]


def approve(user: str, commit: str = HEAD, at: str = "2026-11-10T11:00:00Z") -> Review:
    return Review(user, "APPROVED", commit, at)


def test_approval_on_current_head_with_green_checks_is_approved():
    decision = evaluate_approval(
        evidence(reviews=[approve("rev")], checks=GREEN), POLICY, expected_head=HEAD
    )
    assert decision.approved and decision.approvers == ["rev"]


def test_stale_approval_on_older_commit_does_not_count():
    decision = evaluate_approval(
        evidence(reviews=[approve("rev", OLD)], checks=GREEN), POLICY, expected_head=HEAD
    )
    assert not decision.approved
    assert decision.stale_approvals == [f"rev@{OLD[:10]}"]


def test_latest_review_per_user_wins():
    blocked = evaluate_approval(
        evidence(
            reviews=[approve("rev", at="t1"), Review("rev", "CHANGES_REQUESTED", HEAD, "t2")],
            checks=GREEN,
        ),
        POLICY,
        expected_head=HEAD,
    )
    assert not blocked.approved and blocked.changes_requested_by == ["rev"]
    cleared = evaluate_approval(
        evidence(
            reviews=[Review("rev", "CHANGES_REQUESTED", OLD, "t1"), approve("rev", at="t2")],
            checks=GREEN,
        ),
        POLICY,
        expected_head=HEAD,
    )
    assert cleared.approved


def test_any_open_change_request_blocks_even_with_enough_approvals():
    decision = evaluate_approval(
        evidence(
            reviews=[approve("a"), approve("b"), Review("c", "CHANGES_REQUESTED", HEAD, "t")],
            checks=GREEN,
        ),
        POLICY,
        expected_head=HEAD,
    )
    assert not decision.approved


def test_author_self_approval_is_ignored():
    decision = evaluate_approval(
        evidence(reviews=[approve("author")], checks=GREEN), POLICY, expected_head=HEAD
    )
    assert not decision.approved and decision.approvers == []


@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [
        ({"draft": True}, "draft"),
        ({"state": "closed"}, "closed"),
        ({"merged": True, "state": "closed"}, "merged"),
        ({"head": OLD}, "not the verified release commit"),
    ],
)
def test_pr_state_preconditions(kwargs, fragment):
    decision = evaluate_approval(
        evidence(reviews=[approve("rev", kwargs.get("head", HEAD))], checks=GREEN, **kwargs),
        POLICY,
        expected_head=HEAD,
    )
    assert not decision.approved
    assert any(fragment in r for r in decision.reasons)


def test_draft_allowed_only_when_policy_says_so():
    lenient = ApprovalPolicy(1, ("ci",), require_ready_for_review=False)
    assert evaluate_approval(
        evidence(reviews=[approve("rev")], checks=GREEN, draft=True), lenient, expected_head=HEAD
    ).approved


@pytest.mark.parametrize(
    ("checks", "state"),
    [
        ([], "missing"),
        ([CheckRun("ci", "in_progress", None, "t")], "in_progress"),
        ([CheckRun("ci", "completed", "failure", "t")], "failure"),
    ],
)
def test_required_checks_must_succeed(checks, state):
    decision = evaluate_approval(
        evidence(reviews=[approve("rev")], checks=checks), POLICY, expected_head=HEAD
    )
    assert not decision.approved and decision.checks == {"ci": state}


def test_latest_check_run_wins():
    failed_then_fixed = [
        CheckRun("ci", "completed", "failure", "2026-11-10T01:00:00Z"),
        CheckRun("ci", "completed", "success", "2026-11-10T02:00:00Z"),
    ]
    assert evaluate_approval(
        evidence(reviews=[approve("rev")], checks=failed_then_fixed), POLICY, expected_head=HEAD
    ).approved
    passed_then_broke = list(
        reversed(
            [
                CheckRun("ci", "completed", "failure", "2026-11-10T03:00:00Z"),
                CheckRun("ci", "completed", "success", "2026-11-10T02:00:00Z"),
            ]
        )
    )
    assert not evaluate_approval(
        evidence(reviews=[approve("rev")], checks=passed_then_broke), POLICY, expected_head=HEAD
    ).approved


# --------------------------------------------------------------------------- reader
def _transport(responses):  # type: ignore[no-untyped-def]
    calls = []

    def transport(method, url, headers, body):  # type: ignore[no-untyped-def]
        calls.append((method, url, dict(headers)))
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return transport, calls


def test_reader_does_not_retry_access_errors_and_never_leaks_token():
    transport, calls = _transport([(404, {"message": "Not Found"})])
    reader = GitHubReader(token="secret-token", transport=transport, sleep=lambda _s: None)
    with pytest.raises(GitHubAccessError) as exc:
        reader.pull("o/r", 1)
    assert len(calls) == 1 and "secret-token" not in str(exc.value.to_dict())
    assert calls[0][0] == "GET"  # read-only


def test_reader_retries_transient_failures_with_backoff():
    pr = {
        "number": 1,
        "state": "open",
        "draft": True,
        "merged": False,
        "head": {"ref": "h", "sha": HEAD},
        "base": {"ref": "main"},
        "user": {"login": "u"},
        "html_url": "https://github.com/o/r/pull/1",
    }
    sleeps: list[float] = []
    transport, calls = _transport(
        [(502, {"message": "Bad gateway"}), IntegrationError("connection reset"), (200, pr)]
    )
    status = GitHubReader(transport=transport, sleep=sleeps.append).pull("o/r", 1)
    assert status.draft and status.head_sha == HEAD and sleeps == [1.0, 2.0] and len(calls) == 3


def test_reader_gives_up_after_bounded_attempts():
    transport, _ = _transport([(503, {})] * 3)
    with pytest.raises(IntegrationError) as exc:
        GitHubReader(transport=transport, sleep=lambda _s: None).pull("o/r", 1)
    assert exc.value.details["retryable"] is True


def test_anonymous_reader_sends_no_authorization_header(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    transport, calls = _transport([(404, {"message": "Not Found"})])
    with pytest.raises(GitHubAccessError) as exc:
        GitHubReader(transport=transport).pull("o/r", 1)
    assert "Authorization" not in calls[0][2]
    assert "no GITHUB_TOKEN set" in exc.value.message


@pytest.mark.parametrize("bad", ["o", "o/r/x", "/r"])
def test_repository_names_are_validated(bad):
    with pytest.raises(ValueError):
        GitHubReader(transport=lambda *a: (200, {})).pull(bad, 1)


# --------------------------------------------------------------------------- fail-closed rules
@pytest.mark.parametrize("level", ["read", "triage", "none", "unverified (403)"])
def test_approval_without_verified_write_permission_does_not_count(level):
    decision = evaluate_approval(
        evidence(reviews=[approve("rev")], checks=GREEN, permissions={"rev": level}),
        POLICY,
        expected_head=HEAD,
    )
    assert not decision.approved and decision.unauthorized_approvals == [f"rev ({level})"]


def test_dismissed_review_revokes_earlier_approval():
    decision = evaluate_approval(
        evidence(
            reviews=[approve("rev", at="t1"), Review("rev", "DISMISSED", HEAD, "t2")], checks=GREEN
        ),
        POLICY,
        expected_head=HEAD,
    )
    assert not decision.approved and decision.approvers == []


def test_commit_status_can_satisfy_a_required_check():
    ok = evaluate_approval(
        evidence(reviews=[approve("rev")], statuses={"ci": "success"}), POLICY, expected_head=HEAD
    )
    pending = evaluate_approval(
        evidence(reviews=[approve("rev")], statuses={"ci": "pending"}), POLICY, expected_head=HEAD
    )
    assert ok.approved and not pending.approved and pending.checks == {"ci": "pending"}


def test_branch_protection_checks_are_added_when_readable():
    decision = evaluate_approval(
        evidence(
            reviews=[approve("rev")],
            checks=GREEN,
            protection=["ci", "security-scan"],
            protection_note="read",
        ),
        POLICY,
        expected_head=HEAD,
    )
    assert not decision.approved and decision.checks["security-scan"] == "missing"


def test_unreadable_branch_protection_is_never_treated_as_unprotected():
    strict = ApprovalPolicy(1, ("ci",), require_branch_protection_evidence=True)
    decision = evaluate_approval(
        evidence(reviews=[approve("rev")], checks=GREEN), strict, expected_head=HEAD
    )
    assert not decision.approved
    assert any("branch protection could not be read" in r for r in decision.reasons)
    # Without the requirement the configured policy still applies in full (no inference).
    assert evaluate_approval(
        evidence(reviews=[approve("rev")], checks=GREEN), POLICY, expected_head=HEAD
    ).approved


def test_reader_records_unverifiable_permission_and_protection():
    pr = {
        "number": 1,
        "state": "open",
        "draft": False,
        "merged": False,
        "head": {"ref": "h", "sha": HEAD},
        "base": {"ref": "main"},
        "user": {"login": "a"},
        "html_url": "https://github.com/o/r/pull/1",
    }
    routes = {
        "/pulls/1": (200, pr),
        "/pulls/1/reviews": (
            200,
            [
                {
                    "user": {"login": "rev"},
                    "state": "APPROVED",
                    "commit_id": HEAD,
                    "submitted_at": "t",
                }
            ],
        ),
        "/check-runs": (200, {"check_runs": []}),
        "/status": (200, {"statuses": []}),
        "/permission": (401, {"message": "Requires authentication"}),
        "/protection": (403, {"message": "Resource not accessible"}),
    }

    def transport(method, url, headers, body):  # type: ignore[no-untyped-def]
        path = url.split("?")[0]
        for suffix, response in routes.items():
            if path.endswith(suffix):
                return response
        return 404, {}

    ev = GitHubReader(transport=transport, sleep=lambda _s: None).evidence("o/r", 1)
    assert ev.permissions == {"rev": "unverified (401)"}
    assert ev.protection_checks is None and ev.protection_note == "unverified (403)"
    assert not evaluate_approval(ev, ApprovalPolicy(1, ()), expected_head=HEAD).approved
