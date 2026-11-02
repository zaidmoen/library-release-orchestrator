"""End-to-end scenarios A-F against real, temporary Git repositories."""

from __future__ import annotations

import json

import pytest

from release_orchestrator.errors import ApprovalRequiredError, BlockedError
from release_orchestrator.models import Verdict
from release_orchestrator.planning.manifest import load_plan, verify_release_branch
from release_orchestrator.state import RunStatus
from release_orchestrator.workflow import RequestInput, write_plan
from tests.conftest import snapshot_repo

pytestmark = pytest.mark.integration


def _release(demo, plan):  # type: ignore[no-untyped-def]
    demo.orch.prepare(plan, approve=True, dry_run=False)
    demo.orch.validate(plan)
    demo.orch.open_pr(plan, approve=True)
    return demo.orch.publish(plan, approve=True)


# --------------------------------------------------------------------- Scenario A
def test_scenario_a_independent_out_of_order(demo_factory):
    demo = demo_factory("independent")
    plan, run = demo.plan("FIN-204")

    assert plan.status == "PLAN_READY" and run.state is RunStatus.PLAN_READY
    assert plan.analysis["verdict"] == Verdict.INDEPENDENT
    assert plan.included_keys == ["FIN-204"]
    assert [c.key for c in plan.excluded] == ["FIN-198"]
    assert plan.proposed_version == "1.1.0"
    assert len(plan.included_commits) == 2  # multi-commit change (subject + Refs: trailer)

    result = demo.orch.prepare(plan, approve=True, dry_run=False)
    assert result["status"] == "prepared"
    files = demo.git("ZaidLibrary", "ls-tree", "-r", "--name-only", "release/1.1.0").split()
    assert "src/zaidlibrary/currency.py" not in files  # Change A is NOT in the release
    assert "tests/test_tax.py" in files
    payments = demo.git("ZaidLibrary", "show", "release/1.1.0:src/zaidlibrary/payments.py")
    assert "calculate_tax" in payments
    init = demo.git("ZaidLibrary", "show", "release/1.1.0:src/zaidlibrary/__init__.py")
    assert '__version__ = "1.1.0"' in init
    # Provenance recorded with cherry-pick -x trailers.
    log = demo.git("ZaidLibrary", "log", "--format=%B", "v1.0.0..release/1.1.0")
    for sha in plan.included_shas:
        assert f"(cherry picked from commit {sha})" in log

    assert demo.orch.validate(plan)["status"] == "PASSED"
    assert demo.orch.status(plan).state is RunStatus.PR_READY


def test_second_release_after_out_of_order(demo_factory):
    demo = demo_factory("independent")
    plan1, _ = demo.plan("FIN-204")
    _release(demo, plan1)

    plan2, _ = demo.plan("FIN-198")  # base defaults to the latest tag: v1.1.0
    assert plan2.base_ref == "v1.1.0"
    assert plan2.proposed_version == "1.2.0"
    assert "FIN-204" in plan2.already_released
    assert plan2.included_keys == ["FIN-198"]
    _release(demo, plan2)
    files = demo.git("ZaidLibrary", "ls-tree", "-r", "--name-only", "release/1.2.0").split()
    assert {"src/zaidlibrary/currency.py", "tests/test_tax.py"} <= set(files)


def test_normal_release_all_changes(demo_factory):
    demo = demo_factory("independent")
    plan, _ = demo.plan("FIN-198", "FIN-204")
    assert plan.status == "PLAN_READY" and plan.excluded == []
    assert _release(demo, plan)["status"] == "published"
    assert demo.git("ZaidLibrary", "rev-parse", "v1.1.0") == demo.git(
        "ZaidLibrary", "rev-parse", "release/1.1.0")


def test_prepare_does_not_touch_user_worktree(demo_factory):
    demo = demo_factory("independent")
    (demo.lib / "scratch.txt").write_text("uncommitted user work\n")
    head_before = demo.git("ZaidLibrary", "rev-parse", "HEAD")
    status_before = demo.git("ZaidLibrary", "status", "--porcelain")
    plan, _ = demo.plan("FIN-204")
    demo.orch.prepare(plan, approve=True, dry_run=False)
    assert demo.git("ZaidLibrary", "rev-parse", "HEAD") == head_before
    assert demo.git("ZaidLibrary", "status", "--porcelain") == status_before
    assert demo.git("ZaidLibrary", "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert demo.git("ZaidLibrary", "worktree", "list").count("\n") == 0


# --------------------------------------------------------------------- Scenario B
def test_scenario_b_dependent_blocked(demo_factory):
    demo = demo_factory("dependent")
    plan, run = demo.plan("FIN-204")

    assert plan.analysis["verdict"] == Verdict.DEPENDENT
    assert plan.status == "BLOCKED" and run.state is RunStatus.BLOCKED
    reasons = {b["reason"] for b in plan.blockers}
    assert "MISSING_PREREQUISITE" in reasons
    assert all(b["depends_on"] == "FIN-198" for b in plan.blockers)
    # Git alone would have accepted it: the textual cherry-pick is clean.
    assert plan.analysis["probe"]["applied_cleanly"] is True
    assert plan.prerequisites == ["FIN-198"]
    options = {r["option"]: r for r in plan.remediation}
    assert options["include_prerequisites"]["changes"] == ["FIN-198", "FIN-204"]
    with pytest.raises(BlockedError) as exc:
        demo.orch.prepare(plan, approve=True, dry_run=False)
    assert exc.value.reason == "PLAN_BLOCKED"
    assert "release/" not in demo.git("ZaidLibrary", "branch")


def test_scenario_b_remediation_plan_is_ready(demo_factory):
    demo = demo_factory("dependent")
    plan, _ = demo.plan("FIN-198", "FIN-204")
    assert plan.status == "PLAN_READY"


# --------------------------------------------------------------------- Scenario C
def test_scenario_c_conflict_detected_and_repo_preserved(demo_factory):
    demo = demo_factory("conflict")
    before = snapshot_repo(demo.lib)
    plan, _ = demo.plan("FIN-204")
    assert plan.analysis["verdict"] == Verdict.CONFLICT
    conflict = next(b for b in plan.blockers if b["reason"] == "GIT_CONFLICT")
    assert conflict["depends_on"] == "FIN-198"
    probe = plan.analysis["probe"]
    assert probe["conflicted_files"] == ["src/zaidlibrary/payments.py"]
    assert {r["option"] for r in plan.remediation} >= {"backport_commit",
                                                       "include_conflicting_changes"}
    assert snapshot_repo(demo.lib) == before


def test_scenario_c_conflict_during_prepare_leaves_no_branch(demo_factory):
    """Even if a plan claims READY (e.g. base moved), prepare aborts cleanly."""
    demo = demo_factory("conflict")
    plan, _ = demo.plan("FIN-198", "FIN-204")  # coherent plan
    plan.apply_order.reverse()  # force an impossible order
    plan.plan_id = ""  # recompute identity for the tampered plan
    plan.__post_init__()
    run, _ = demo.orch.s.runs.create_or_load(plan.plan_id, plan.library, plan.proposed_version)
    run.transition(RunStatus.PLAN_READY)
    demo.orch.s.runs.save(run)
    before = snapshot_repo(demo.lib)
    with pytest.raises(BlockedError) as exc:
        demo.orch.prepare(plan, approve=True, dry_run=False)
    assert exc.value.reason == "GIT_CONFLICT"
    after = snapshot_repo(demo.lib)
    assert after["refs"] == before["refs"]
    assert after["worktrees"] == before["worktrees"]
    assert after["status"] == before["status"]
    assert demo.orch.status(plan).state is RunStatus.BLOCKED


# --------------------------------------------------------------------- Scenario D
def test_scenario_d_consumer_updates(demo_factory):
    demo = demo_factory("independent")
    plan, _ = demo.plan("FIN-204")
    actions = {c["name"]: c["action"] for c in plan.consumers}
    assert actions == {"payment-service": "update", "invoice-service": "not_targeted"}
    invoice_before = snapshot_repo(demo.root / "invoice-service")

    _release(demo, plan)
    summary = demo.orch.update_consumers(plan, approve=True, dry_run=False)

    [update] = summary["updated"]
    assert update["consumer"] == "payment-service"
    assert update["validation"]["status"] == "PASSED"
    assert update["pull_request"]["created"] is True
    pinned = demo.git("payment-service", "show", "deps/zaidlibrary-1.1.0:requirements.txt")
    assert pinned == "zaidlibrary==1.1.0"
    assert snapshot_repo(demo.root / "invoice-service") == invoice_before
    assert demo.orch.status(plan).state is RunStatus.COMPLETED


def test_consumer_update_requires_published_version(demo_factory):
    demo = demo_factory("independent")
    plan, _ = demo.plan("FIN-204")
    demo.orch.prepare(plan, approve=True, dry_run=False)
    demo.orch.validate(plan)
    with pytest.raises(BlockedError) as exc:
        demo.orch.update_consumers(plan, approve=True, dry_run=False)
    assert exc.value.reason == "VERSION_NOT_PUBLISHED"
    assert "deps/" not in demo.git("payment-service", "branch")


# --------------------------------------------------------------------- Scenario E
def test_scenario_e_duplicate_execution(demo_factory):
    demo = demo_factory("independent")
    plan_a, _ = demo.plan("FIN-204")
    plan_b, _ = demo.plan("FIN-204")
    assert plan_a.plan_id == plan_b.plan_id
    assert plan_a.to_json() == plan_b.to_json()  # byte-identical, deterministic

    _release(demo, plan_a)
    demo.orch.update_consumers(plan_a, approve=True, dry_run=False)
    branches = demo.git("ZaidLibrary", "branch")
    commits = demo.git("ZaidLibrary", "rev-list", "--all", "--count")

    assert demo.orch.prepare(plan_b, approve=True, dry_run=False)["status"] == "already_prepared"
    assert demo.orch.open_pr(plan_b, approve=True)["created"] is False
    assert demo.orch.publish(plan_b, approve=True)["status"] == "already_published"
    again = demo.orch.update_consumers(plan_b, approve=True, dry_run=False)
    assert again["updated"][0]["status"] == "already_prepared"
    assert again["updated"][0]["pull_request"]["created"] is False
    assert demo.git("ZaidLibrary", "branch") == branches
    assert demo.git("ZaidLibrary", "rev-list", "--all", "--count") == commits


# --------------------------------------------------------------------- Scenario F
def test_scenario_f_failed_validation(demo_factory):
    demo = demo_factory("failing")
    plan, _ = demo.plan("FIN-204")
    # Static analysis can only warn: FIN-204 uses a function FIN-198 modified.
    assert plan.analysis["verdict"] == Verdict.INDEPENDENT_WITH_RISKS
    kinds = {f["kind"] for f in plan.analysis["findings"]}
    assert "SHARED_SYMBOL_MODIFIED" in kinds
    demo.orch.prepare(plan, approve=True, dry_run=False)

    with pytest.raises(BlockedError) as exc:
        demo.orch.validate(plan)
    assert exc.value.reason == "VALIDATION_FAILED"
    result = exc.value.details
    assert result["status"] == "FAILED"
    assert "1.13" in result["results"][0]["output_tail"]  # real test output, not a summary
    assert demo.orch.status(plan).state is RunStatus.BLOCKED
    with pytest.raises(BlockedError):
        demo.orch.open_pr(plan, approve=True)
    with pytest.raises(BlockedError):
        demo.orch.publish(plan, approve=True)


# --------------------------------------------------------------------- safety
def test_dry_run_is_non_mutating(demo_factory):
    demo = demo_factory("independent")
    repos = [demo.lib, demo.root / "payment-service", demo.root / "invoice-service"]
    plan, _ = demo.plan("FIN-204")  # planning writes only orchestrator state, never repos
    before = [snapshot_repo(r) for r in repos]
    demo.orch.analyze(RequestInput(tickets=("FIN-204",)))
    result = demo.orch.prepare(plan, approve=False, dry_run=True)
    assert result["status"] == "dry_run_ok"
    assert result["verification"]["ok"] is True
    assert [snapshot_repo(r) for r in repos] == before


def test_approval_required_for_writes(demo_factory):
    demo = demo_factory("independent")
    plan, _ = demo.plan("FIN-204")
    with pytest.raises(ApprovalRequiredError):
        demo.orch.prepare(plan, approve=False, dry_run=False)
    assert demo.orch.status(plan).state is RunStatus.APPROVAL_REQUIRED
    assert "release/" not in demo.git("ZaidLibrary", "branch")


def test_verify_detects_extra_commit(demo_factory):
    demo = demo_factory("independent")
    plan, _ = demo.plan("FIN-204")
    demo.orch.prepare(plan, approve=True, dry_run=False)
    # Someone sneaks Change A onto the release branch.
    wt = demo.root / "tamper"
    demo.git("ZaidLibrary", "worktree", "add", str(wt), "release/1.1.0")
    sha_a = demo.git("ZaidLibrary", "log", "--format=%H", "--grep=FIN-198", "main")
    demo.git(wt, "-c", "user.name=x", "-c", "user.email=x@example.invalid",
             "cherry-pick", sha_a)
    report = verify_release_branch(demo.orch.s.git, plan)
    assert report.ok is False
    failed = {c["check"] for c in report.checks if not c["ok"]}
    assert "commit_count" in failed
    with pytest.raises(BlockedError) as exc:
        demo.orch.validate(plan)
    assert exc.value.reason == "MANIFEST_MISMATCH"


def test_existing_branch_with_other_content_is_never_overwritten(demo_factory):
    demo = demo_factory("independent")
    demo.git("ZaidLibrary", "branch", "release/1.1.0", "main")  # unrelated pre-existing branch
    plan, _ = demo.plan("FIN-204")
    with pytest.raises(BlockedError) as exc:
        demo.orch.prepare(plan, approve=True, dry_run=False)
    assert exc.value.reason == "BRANCH_EXISTS_MISMATCH"
    assert demo.git("ZaidLibrary", "rev-parse", "release/1.1.0") == demo.git(
        "ZaidLibrary", "rev-parse", "main")


def test_recovery_after_failed_prepare(demo_factory):
    demo = demo_factory("independent")
    demo.git("ZaidLibrary", "branch", "release/1.1.0", "main")
    plan, _ = demo.plan("FIN-204")
    with pytest.raises(BlockedError):
        demo.orch.prepare(plan, approve=True, dry_run=False)
    assert demo.orch.status(plan).state is RunStatus.BLOCKED
    # A human renames the stale branch (outside the orchestrator); the run then resumes.
    demo.git("ZaidLibrary", "branch", "-m", "release/1.1.0", "stale/1.1.0")
    assert demo.orch.prepare(plan, approve=True, dry_run=False)["status"] == "prepared"
    assert demo.orch.validate(plan)["status"] == "PASSED"
    assert demo.orch.status(plan).state is RunStatus.PR_READY


def test_report_traceability(demo_factory):
    demo = demo_factory("independent")
    plan, _ = demo.plan("FIN-204")
    _release(demo, plan)
    demo.orch.update_consumers(plan, approve=True, dry_run=False)
    report = json.loads((demo.config.state_dir / "reports" / f"{plan.plan_id}.json").read_text())
    steps = report["run"]["steps"]
    sources = {p["source"] for p in steps["prepare"]["result"]["provenance"]}
    assert sources == set(plan.included_shas)
    head = steps["prepare"]["result"]["head"]
    assert steps["publish"]["result"]["artifact"]["metadata"]["release_commit"] == head
    assert steps["publish"]["result"]["artifact"]["metadata"]["changes"] == ["FIN-204"]
    assert steps["update-consumers"]["result"]["updated"][0]["consumer"] == "payment-service"
    md = (demo.config.state_dir / "reports" / f"{plan.plan_id}.md").read_text()
    assert "FIN-204" in md and "COMPLETED" in md


def test_plan_file_roundtrip_and_tamper_detection(demo_factory, tmp_path):
    demo = demo_factory("independent")
    plan, _ = demo.plan("FIN-204")
    path, _ = write_plan(plan, tmp_path / "plan.json")
    assert load_plan(path, demo.config).plan_id == plan.plan_id
    data = json.loads(path.read_text())
    data["validation"]["required"][0]["argv"] = ["sh", "-c", "echo pwned"]
    path.write_text(json.dumps(data))
    from release_orchestrator.errors import ManifestError

    with pytest.raises(ManifestError):
        load_plan(path, demo.config)


def test_collision_with_tag_blocks(demo_factory):
    """A concurrent release tags v1.1.0 elsewhere after we planned: publish must refuse."""
    demo = demo_factory("independent")
    plan, _ = demo.plan("FIN-204")
    demo.orch.prepare(plan, approve=True, dry_run=False)
    demo.orch.validate(plan)
    demo.git("ZaidLibrary", "tag", "v1.1.0", "main")  # someone else's 1.1.0
    with pytest.raises(BlockedError) as exc:
        demo.orch.publish(plan, approve=True)
    assert exc.value.reason == "VERSION_COLLISION"
    assert demo.orch.s.registry.list_versions("zaidlibrary") == []
    # Re-planning sees the new tag and moves on to the next free version.
    replanned, _ = demo.plan("FIN-204", base="v1.0.0")
    assert replanned.proposed_version == "1.2.0"
