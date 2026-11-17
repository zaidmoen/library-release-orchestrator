"""Issue-tracker release eligibility over a timestamped snapshot (ADR-0013)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from release_orchestrator.errors import ConfigError
from release_orchestrator.integrations.jira import (
    EligibilityPolicy,
    FixtureIssueTracker,
    check_eligibility,
)

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


def tracker(tmp_path: Path, body: str, fetched_at: str | None = "2026-11-10T11:55:00Z"):  # type: ignore[no-untyped-def]
    path = tmp_path / "snapshot.yaml"
    header = f"fetched_at: {fetched_at}\n" if fetched_at else ""
    path.write_text(header + "issues:\n" + body, encoding="utf-8")
    return FixtureIssueTracker(path)


ISSUES = """  FIN-198: {summary: A, status: In Progress}
  FIN-204: {summary: B, status: Ready for release}
  FIN-211: {summary: C, status: Ready for release, blocked_by: [FIN-198]}
  FIN-215: {summary: D, status: Done}
  FIN-219: {summary: E, status: Ready for release, blocked_by: [FIN-215]}
  FIN-300: {summary: F, status: Ready for release, blocked_by: [FIN-301]}
  FIN-301: {summary: G, status: Ready for release, blocked_by: [FIN-300]}
  FIN-400: {summary: H, status: Ready for release, accessible: false}
"""
POLICY = EligibilityPolicy(release_statuses=("Ready for release",), max_age_minutes=30)


def reasons(findings):  # type: ignore[no-untyped-def]
    return sorted((f.reason, f.key) for f in findings)


def test_independent_ready_issue_is_eligible(tmp_path):
    assert (
        check_eligibility(
            tracker(tmp_path, ISSUES), ["FIN-204"], released=set(), policy=POLICY, now=NOW
        )
        == []
    )


def test_unresolved_blocker_blocks_fin_211(tmp_path):
    findings = check_eligibility(
        tracker(tmp_path, ISSUES), ["FIN-211"], released=set(), policy=POLICY, now=NOW
    )
    assert reasons(findings) == [("UNRESOLVED_BLOCKER", "FIN-211")]
    assert findings[0].details == {"blocker": "FIN-198"}


@pytest.mark.parametrize(
    ("included", "released"),
    [
        (["FIN-211", "FIN-198"], set()),  # blocker shipped together (status gate applies to it)
        (["FIN-211"], {"FIN-198"}),  # blocker already released in the base
    ],
)
def test_blocker_in_release_or_already_released_is_satisfied(tmp_path, included, released):
    findings = check_eligibility(
        tracker(tmp_path, ISSUES),
        included,
        released=released,
        policy=EligibilityPolicy(max_age_minutes=30),
        now=NOW,
    )
    assert findings == []


def test_resolved_blocker_is_satisfied(tmp_path):
    assert (
        check_eligibility(
            tracker(tmp_path, ISSUES), ["FIN-219"], released=set(), policy=POLICY, now=NOW
        )
        == []
    )


def test_invalid_status_blocks(tmp_path):
    findings = check_eligibility(
        tracker(tmp_path, ISSUES), ["FIN-198"], released=set(), policy=POLICY, now=NOW
    )
    assert reasons(findings) == [("ISSUE_NOT_RELEASABLE", "FIN-198")]


def test_circular_blockers_are_detected_once(tmp_path):
    findings = check_eligibility(
        tracker(tmp_path, ISSUES), ["FIN-300", "FIN-301"], released=set(), policy=POLICY, now=NOW
    )
    cycles = [f for f in findings if f.reason == "ISSUE_LINK_CYCLE"]
    assert len(cycles) == 1 and set(cycles[0].details["cycle"]) == {"FIN-300", "FIN-301"}


def test_inaccessible_issue_blocks(tmp_path):
    findings = check_eligibility(
        tracker(tmp_path, ISSUES), ["FIN-400"], released=set(), policy=POLICY, now=NOW
    )
    assert reasons(findings) == [("ISSUE_INACCESSIBLE", "FIN-400")]


@pytest.mark.parametrize("fetched_at", ["2026-11-10T09:00:00Z", None])
def test_stale_or_undated_snapshot_blocks(tmp_path, fetched_at):
    findings = check_eligibility(
        tracker(tmp_path, ISSUES, fetched_at), ["FIN-204"], released=set(), policy=POLICY, now=NOW
    )
    assert reasons(findings) == [("ISSUE_DATA_STALE", "*")]


def test_blocker_missing_from_snapshot_is_unresolved(tmp_path):
    body = "  FIN-500: {summary: X, status: Ready for release, blocked_by: [FIN-999]}\n"
    findings = check_eligibility(
        tracker(tmp_path, body), ["FIN-500"], released=set(), policy=POLICY, now=NOW
    )
    assert "not in snapshot" in findings[0].message


def test_untrusted_link_values_are_ignored(tmp_path):
    body = (
        "  FIN-600: {summary: X, status: Ready for release, blocked_by: ['FIN-1; rm -rf /', 7]}\n"
    )
    assert tracker(tmp_path, body).get("FIN-600").blocked_by == ()


def test_bad_timestamp_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError):
        tracker(tmp_path, ISSUES, "yesterday")


def test_tracker_and_code_disagreement_is_reported(tmp_path):
    """Code says FIN-211 needs FIN-198; a tracker without the link is a traceability gap."""
    from release_orchestrator.lab import build_lab
    from tests.lab.test_lab_scenarios import Lab

    root = tmp_path / "lab"
    config = build_lab(root)
    jira = root / "jira.yaml"
    jira.write_text(jira.read_text().replace("    blocked_by: [FIN-198]\n", ""))
    lab = Lab(root, config)
    plan = lab.plan("FIN-211")
    assert {b["reason"] for b in plan.blockers} == {"MISSING_PREREQUISITE"}  # code still blocks
    assert any("traceability gap" in r for r in plan.compatibility_risks)
