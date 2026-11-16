"""Release modes (ADR-0014): discovery, Q1 options, Q2/Q3 selection, impact preview.

Read-only tests share one lab (discovery and preview never write). Tests that change the
repository or run state build their own.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from release_orchestrator.cli import main
from release_orchestrator.errors import BlockedError, InvalidInputError, NotFoundError
from release_orchestrator.lab import LIBRARY_DIR, build_lab
from release_orchestrator.modes import BLOCKED, NEEDS_VALIDATION, NOT_SELECTED, READY, ModeEngine
from tests.conftest import snapshot_repo
from tests.lab.test_lab_scenarios import Lab

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def shared(tmp_path_factory: pytest.TempPathFactory) -> Lab:
    root = tmp_path_factory.mktemp("modes") / "lab"
    return Lab(root, build_lab(root))


@pytest.fixture
def lab(tmp_path: Path) -> Lab:
    root = tmp_path / "lab"
    return Lab(root, build_lab(root))


def engine(lab: Lab) -> ModeEngine:
    lab.reload()
    return ModeEngine(lab.orch)


def opts(result):  # type: ignore[no-untyped-def]
    return {o["id"]: o for o in result["options"]}


# --------------------------------------------------------------------------- discovery
def test_discovery_finds_stable_candidates_services_and_environments(shared):
    before = snapshot_repo(shared.lib)
    disc = engine(shared).discover()
    assert disc["latest_stable"]["tag"] == "v1.0.0"
    cands = {c["tag"]: c for c in disc["pending_candidates"]}
    assert cands["v1.1.0-rc.22"]["keys"] == ["FIN-198", "FIN-204"]
    assert cands["v1.1.0-rc.23"]["contains"] == ["v1.1.0-rc.22"]
    assert cands["v1.1.0-rc.23"]["own_keys"] == ["FIN-211"]
    assert {s["name"]: s["current_version"] for s in disc["services"]} == {
        "payment-service": "1.0.0",
        "invoice-service": "1.0.0",
    }
    assert [(e["name"], e["branch"]) for e in disc["environments"]] == [
        ("prep", "prep"),
        ("stage", "stage"),
        ("master", "master"),
    ]
    assert any("no 'origin' remote" in u for u in disc["uncertainties"])  # reported, not assumed
    assert engine(shared).discover()["fingerprint"] == disc["fingerprint"]  # deterministic
    assert snapshot_repo(shared.lib) == before  # read-only


def test_inaccessible_service_is_an_uncertainty_and_blocks_when_selected(tmp_path):
    root = tmp_path / "lab"
    config = build_lab(root)
    config.write_text(
        config.read_text().replace(
            "repo: ./harri-release-lab-invoice", "repo: ./missing-invoice-repo"
        )
    )
    lab = Lab(root, config)
    disc = ModeEngine(lab.orch).discover()
    assert any("invoice-service: repository inaccessible" in u for u in disc["uncertainties"])
    options = ModeEngine(lab.orch).options("release-candidate", tickets=["FIN-204"])
    # Planning itself fails closed: consumer impact can't be assessed for a missing repo.
    [offered] = [o for o in options["options"] if o["id"] != "stop"]
    assert offered["valid"] is False
    preview = ModeEngine(lab.orch).preview(
        "release-candidate",
        tickets=["FIN-204"],
        choice=offered["id"],
        services=["all"],
        environments=None,
    )
    row = next(r for r in preview["services"] if r["service"] == "invoice-service")
    assert row["status"] == BLOCKED and preview["readiness"] == BLOCKED


# --------------------------------------------------------------------------- mode A
def test_rc_mode_recommends_including_the_missing_prerequisite(shared):
    result = engine(shared).options("release-candidate", tickets=["FIN-211"])
    options = opts(result)
    assert options["requested-only"]["valid"] is False
    assert any("FIN-198" in e for e in options["requested-only"]["evidence"])
    assert options["with-prerequisites"]["valid"] is True
    assert options["with-prerequisites"]["steps"][0]["keys"] == ["FIN-198", "FIN-211"]
    assert result["recommended"] == "with-prerequisites" and "stop" in options


def test_rc_mode_independent_request_is_recommended_as_is(shared):
    result = engine(shared).options("release-candidate", tickets=["FIN-204"])
    assert result["recommended"] == "requested"
    assert opts(result)["requested"]["steps"][0]["version"] == "1.1.0-rc.24"


def test_rc_mode_unknown_ticket_is_not_guessed(shared):
    with pytest.raises(NotFoundError):
        engine(shared).options("release-candidate", tickets=["FIN-999"])


def test_rc_mode_issue_link_cycle_blocks(tmp_path):
    root = tmp_path / "lab"
    config = build_lab(root)
    jira = root / "jira.yaml"
    jira.write_text(
        jira.read_text()
        .replace(
            '  FIN-198: {summary: "Currency conversion for cross-border payments", consumers: []}',
            '  FIN-198: {summary: "Currency", consumers: [], blocked_by: [FIN-204]}',
        )
        .replace(
            '  FIN-204: {summary: "Tax-inclusive order totals", consumers: [payment-service]}',
            '  FIN-204: {summary: "Tax", consumers: [payment-service], blocked_by: [FIN-198]}',
        )
    )
    lab = Lab(root, config)
    option = opts(
        ModeEngine(lab.orch).options("release-candidate", tickets=["FIN-198", "FIN-204"])
    )["requested-only"]
    assert option["valid"] is False
    assert {b["reason"] for b in option["steps"][0]["blockers"]} >= {"ISSUE_LINK_CYCLE"}


def test_rc_mode_no_pending_changes_is_a_no_op(lab):
    lab.git(LIBRARY_DIR, "tag", "v1.5.0", "main")  # everything on main is released
    result = engine(lab).options("release-candidate")
    assert result["recommended"] == "stop"
    assert opts(result)["no-op"]["valid"] is False


# --------------------------------------------------------------------------- mode B
def test_release_mode_lists_candidates_with_evidence(shared):
    result = engine(shared).options("release")
    options = opts(result)
    assert options["release:v1.1.0-rc.22"]["valid"] and options["release:v1.1.0-rc.23"]["valid"]
    assert options["release:v1.1.0-rc.23"]["steps"][0]["version"] == "1.1.0"
    assert any("supersedes v1.1.0-rc.22" in e for e in options["release:v1.1.0-rc.23"]["evidence"])
    picked = engine(shared).options("release", candidate="v1.1.0-rc.22")
    assert picked["recommended"] == "release:v1.1.0-rc.22"
    with pytest.raises(NotFoundError):
        engine(shared).options("release", candidate="v9.9.9-rc.1")


def test_release_mode_blocks_wrong_version_progression(lab):
    lab.git(LIBRARY_DIR, "tag", "v1.2.0-rc.1", "main")  # main contains a breaking change
    option = opts(engine(lab).options("release"))["release:v1.2.0-rc.1"]
    assert option["valid"] is False
    assert option["steps"][0]["blockers"][-1]["reason"] == "VERSION_PROGRESSION"


def test_release_mode_existing_final_tag_means_nothing_pending(lab):
    lab.git(LIBRARY_DIR, "tag", "v1.1.0", "v1.1.0-rc.23")
    result = engine(lab).options("release")
    assert result["recommended"] == "stop" and "no-op" in opts(result)
    assert set(engine(lab).discover()["finalized_candidates"]) == {"v1.1.0-rc.22", "v1.1.0-rc.23"}


def test_release_lineage_is_verified_against_the_candidate(lab):
    plan = lab.plan("FIN-198", "FIN-204", base="v1.0.0", source="v1.1.0-rc.22")
    lab.orch.prepare(plan, approve=True, dry_run=False)
    lab.orch.validate(plan)
    result = lab.orch.verify_lineage(plan, "v1.1.0-rc.22")
    assert result["ok"] and result["differing_files"] == ["src/zaidlibrary/__init__.py"]
    with pytest.raises(BlockedError) as exc:
        lab.orch.verify_lineage(plan, "v1.1.0-rc.23")  # different content
    assert exc.value.reason == "LINEAGE_MISMATCH"
    assert "src/zaidlibrary/invoices.py" in exc.value.details["unexpected_files"]
    with pytest.raises(NotFoundError):
        lab.orch.verify_lineage(plan, "v1.1.0-rc.99")


# --------------------------------------------------------------------------- mode C
def test_reorder_rc23_before_rc22_is_blocked_with_evidence(shared):
    result = engine(shared).options("reorder-releases", order=["v1.1.0-rc.23", "v1.1.0-rc.22"])
    options = opts(result)
    requested = options["requested"]
    assert requested["valid"] is False
    assert requested["steps"][0]["status"] == "BLOCKED"
    assert requested["steps"][1]["status"] == "SKIPPED"  # never evaluated past a block
    assert any("FIN-198" in e for e in requested["evidence"])
    assert any("contains v1.1.0-rc.22" in e for e in requested["evidence"])
    dep = options["dependency-order"]
    assert dep["valid"] and [s["version"] for s in dep["steps"]] == ["1.1.0", "1.2.0"]
    assert options["supersede"]["valid"]
    assert result["recommended"] == "dependency-order"


def test_reorder_validation_errors(shared, lab):
    with pytest.raises(NotFoundError):
        engine(shared).options("reorder-releases", order=["v1.1.0-rc.23", "v1.1.0-rc.77"])
    lab.git(LIBRARY_DIR, "tag", "-d", "v1.1.0-rc.22")  # test setup only (plain git)
    assert "no-op" in opts(engine(lab).options("reorder-releases", order=["v1.1.0-rc.23"]))


# --------------------------------------------------------------------------- Q2 / Q3 / preview
def test_selection_validation(shared):
    eng = engine(shared)
    with pytest.raises(InvalidInputError):
        eng.preview(
            "release-candidate",
            tickets=["FIN-204"],
            choice="requested",
            services=["billing-service"],
            environments=None,
        )
    with pytest.raises(InvalidInputError):
        eng.preview(
            "release-candidate",
            tickets=["FIN-204"],
            choice="requested",
            services=None,
            environments=["production"],
        )
    with pytest.raises(InvalidInputError):
        eng.preview(
            "release-candidate",
            tickets=["FIN-204"],
            choice="nonsense",
            services=None,
            environments=None,
        )


def test_preview_statuses_and_scoping(shared):
    preview = engine(shared).preview(
        "release-candidate",
        tickets=["FIN-211"],
        choice="with-prerequisites",
        services=["payment-service"],
        environments=None,
    )
    rows = {r["service"]: r for r in preview["services"]}
    assert rows["zaidlibrary"]["status"] == NEEDS_VALIDATION  # not validated yet
    assert rows["payment-service"]["status"] == READY and not rows["payment-service"]["affected"]
    assert rows["invoice-service"]["status"] == NOT_SELECTED and rows["invoice-service"]["affected"]
    assert any("invoice-service is affected" in w for w in preview["warnings"])
    assert preview["environments"] == []  # safe default: no environment operation
    assert any("NO environment operation" in w for w in preview["warnings"])
    assert not any("update-consumers" in c for c in preview["commands"])  # nobody selected+affected
    assert preview["executed"] is False


def test_environment_rules_in_preview(shared):
    rc = engine(shared).preview(
        "release-candidate",
        tickets=["FIN-204"],
        choice="requested",
        services=["all"],
        environments=["prep", "stage", "master"],
    )
    envs = {e["environment"]: e for e in rc["environments"]}
    assert envs["master"]["status"] == BLOCKED  # RC never reaches master
    assert envs["prep"]["status"] == NEEDS_VALIDATION and envs["prep"]["branch"] == "prep"
    skip = engine(shared).preview(
        "release-candidate",
        tickets=["FIN-204"],
        choice="requested",
        services=["all"],
        environments=["stage"],
    )
    assert skip["environments"][0]["status"] == BLOCKED  # prep skipped: promotion order


def test_blocked_and_stop_choices_produce_blocked_previews(shared):
    blocked = engine(shared).preview(
        "reorder-releases",
        order=["v1.1.0-rc.23", "v1.1.0-rc.22"],
        choice="requested",
        services=None,
        environments=None,
    )
    assert blocked["readiness"] == BLOCKED
    assert any(d["to"] == "FIN-198" for d in blocked["dependencies"]["unresolved"])
    stop = engine(shared).preview("release", choice="stop", services=None, environments=None)
    assert stop["readiness"] == BLOCKED and stop["commands"] == []


def test_release_preview_commands_include_lineage_and_selected_consumers_only(shared):
    preview = engine(shared).preview(
        "release",
        choice="release:v1.1.0-rc.22",
        services=["payment-service"],
        environments=["prep"],
    )
    commands = "\n".join(preview["commands"])
    assert "verify-lineage --plan plan-1.json --candidate v1.1.0-rc.22" in commands
    assert "--consumer payment-service" in commands and "invoice-service" not in commands
    assert "--source v1.1.0-rc.22" in commands and "promote --version 1.1.0 --env prep" in commands


def test_preview_becomes_ready_only_with_validation_evidence(lab):
    eng = engine(lab)
    first = eng.preview(
        "release-candidate",
        tickets=["FIN-204"],
        choice="requested",
        services=["payment-service"],
        environments=None,
    )
    assert first["services"][0]["status"] == NEEDS_VALIDATION
    plan = lab.plan("FIN-204", base="v1.0.0", prerelease="rc")
    assert plan.plan_id == first["choice"]["steps"][0]["plan_id"]  # the command reproduces it
    lab.orch.prepare(plan, approve=True, dry_run=False)
    lab.orch.validate(plan)
    after = engine(lab).preview(
        "release-candidate",
        tickets=["FIN-204"],
        choice="requested",
        services=["payment-service"],
        environments=None,
    )
    assert after["services"][0]["status"] == READY


def test_approval_policy_keeps_library_unverified_until_pr_status(lab):
    config = lab.config_path
    config.write_text(config.read_text() + "approval_policy:\n  required_approvals: 1\n")
    plan = lab.plan("FIN-204", base="v1.0.0", prerelease="rc")
    lab.orch.prepare(plan, approve=True, dry_run=False)
    lab.orch.validate(plan)
    preview = engine(lab).preview(
        "release-candidate",
        tickets=["FIN-204"],
        choice="requested",
        services=None,
        environments=None,
    )
    assert preview["approval"]["required"] and not preview["approval"]["verified"]
    assert preview["services"][0]["status"] == NEEDS_VALIDATION
    assert any("pr-status" in c for c in preview["commands"])


def test_stale_preview_is_rejected(lab):
    preview = engine(lab).preview(
        "release", choice="release:v1.1.0-rc.22", services=None, environments=None
    )
    assert engine(lab).check_preview(preview)["fresh"] is True
    lab.git(LIBRARY_DIR, "tag", "v1.1.0-rc.24", "main")  # someone cut another candidate
    with pytest.raises(BlockedError) as exc:
        engine(lab).check_preview(preview)
    assert exc.value.reason == "STALE_PREVIEW"


def test_update_consumers_respects_the_selection(lab):
    plan = lab.plan("FIN-204")
    lab.release(plan)
    with pytest.raises(InvalidInputError):
        lab.orch.update_consumers(plan, approve=True, dry_run=False, only=("billing",))
    summary = lab.orch.update_consumers(plan, approve=True, dry_run=False, only=())
    assert summary["updated"] == []
    assert {
        "consumer": "payment-service",
        "action": "not_selected",
        "reasons": ["targeted by the plan but not selected for this operation"],
    } in summary["not_updated"]
    assert "deps/" not in lab.git("harri-release-lab-payment", "branch")


def test_cli_release_mode_end_to_end(shared, capsys, tmp_path):
    cfg = ["--json", "--config", str(shared.config_path), "release-mode"]
    assert main([*cfg, "options"]) == 2  # --mode required
    capsys.readouterr()
    out = tmp_path / "preview.json"
    code = main(
        [
            *cfg,
            "preview",
            "--mode",
            "reorder-releases",
            "--order",
            "v1.1.0-rc.23,v1.1.0-rc.22",
            "--choice",
            "requested",
            "--out",
            str(out),
        ]
    )
    assert code == 3  # blocked preview exits 3
    capsys.readouterr()
    assert json.loads(out.read_text())["readiness"] == BLOCKED
    assert "| zaidlibrary |" in out.with_suffix(".md").read_text()
    assert main([*cfg, "check-preview", "--preview", str(out)]) == 0


def test_fin_204_and_fin_211_regressions_still_hold(shared):
    eng = engine(shared)
    assert opts(eng.options("release-candidate", tickets=["FIN-204"]))["requested"]["valid"]
    assert not opts(eng.options("release-candidate", tickets=["FIN-211"]))["requested-only"][
        "valid"
    ]
    subprocess.run(["git", "-C", str(shared.lib), "status"], check=True, capture_output=True)


# --------------------------------------------------------------------------- acceptance gaps
def test_preview_always_shows_environment_branch_mapping_and_steps(shared):
    from release_orchestrator.modes import render_preview

    preview = engine(shared).preview(
        "reorder-releases",
        order=["v1.1.0-rc.23", "v1.1.0-rc.22"],
        choice="dependency-order",
        services=["all"],
        environments=None,
    )
    mapping = {
        (e["environment"], e["branch"], e["selected"]) for e in preview["environment_mapping"]
    }
    assert mapping == {
        ("prep", "prep", False),
        ("stage", "stage", False),
        ("master", "master", False),
    }
    assert [(s["candidate"], s["version"]) for s in preview["release_steps"]] == [
        ("v1.1.0-rc.22", "1.1.0"),
        ("v1.1.0-rc.23", "1.2.0"),
    ]
    md = render_preview(preview)
    for expected in (
        "## Environments and branches",
        "| prep | prep |",
        "| master | master |",
        "| v1.1.0-rc.22 | FIN-198, FIN-204 |",
        "| 2 | v1.1.0-rc.23 | FIN-211 | 1.2.0",
        "Latest stable release: **1.0.0**",
        "Affected microservices:",
        "Nothing has been executed",
    ):
        assert expected in md, expected


def test_blocked_environment_is_listed_as_a_blocker(shared):
    preview = engine(shared).preview(
        "release-candidate",
        tickets=["FIN-211"],
        choice="with-prerequisites",
        services=["all"],
        environments=["prep", "stage", "master"],
    )
    assert preview["readiness"] == BLOCKED
    assert "environment master: master doesn't accept pre-releases" in preview["blockers"]


def test_reordering_finalized_candidates_explains_why(lab):
    lab.git(LIBRARY_DIR, "tag", "v1.1.0", "v1.1.0-rc.23")
    with pytest.raises(BlockedError) as exc:
        engine(lab).options("reorder-releases", order=["v1.1.0-rc.23", "v1.1.0-rc.22"])
    assert exc.value.reason == "ALREADY_FINALIZED" and "v1.1.0" in exc.value.message


def test_q1_lists_the_releases_and_what_each_option_produces(shared):
    """Engineer feedback: Q1 must show the releases themselves (stable, pending RCs and
    their contents) and the release(s) each option would produce, in every mode."""
    eng = engine(shared)
    for args in (
        {"mode": "release-candidate", "tickets": ["FIN-211"]},
        {"mode": "release"},
        {"mode": "reorder-releases", "order": ["v1.1.0-rc.23", "v1.1.0-rc.22"]},
    ):
        result = eng.options(**args)
        rel = result["releases"]
        assert rel["latest_stable"]["tag"] == "v1.0.0"
        pending = {c["tag"]: c for c in rel["pending_candidates"]}
        assert pending["v1.1.0-rc.22"]["tickets"] == ["FIN-198", "FIN-204"]
        assert pending["v1.1.0-rc.23"]["own_tickets"] == ["FIN-211"]
        assert pending["v1.1.0-rc.23"]["contains"] == ["v1.1.0-rc.22"]
        text = result["releases_text"]
        assert "Latest stable: v1.0.0" in text
        assert "v1.1.0-rc.22 (FIN-198, FIN-204)" in text
        assert "contains v1.1.0-rc.22" in text

    rc = opts(eng.options("release-candidate", tickets=["FIN-211"]))
    assert [p["version"] for p in rc["with-prerequisites"]["produces"]] == ["1.1.0-rc.24"]
    release = opts(eng.options("release"))
    assert release["release:v1.1.0-rc.22"]["produces"][0]["candidate"] == "v1.1.0-rc.22"
    assert release["release:v1.1.0-rc.22"]["produces"][0]["version"] == "1.1.0"
    order = opts(eng.options("reorder-releases", order=["v1.1.0-rc.23", "v1.1.0-rc.22"]))
    assert [(p["candidate"], p["version"]) for p in order["dependency-order"]["produces"]] == [
        ("v1.1.0-rc.22", "1.1.0"),
        ("v1.1.0-rc.23", "1.2.0"),
    ]
    assert [p["status"] for p in order["requested"]["produces"]] == ["BLOCKED", "SKIPPED"]
    assert opts(eng.options("release"))["stop"]["produces"] == []
