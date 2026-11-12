"""S01-S15 against the release lab (real temporary Git repositories). See docs/release-lab.md."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from release_orchestrator.config import load_config
from release_orchestrator.environments import EnvironmentLedger
from release_orchestrator.errors import (
    ApprovalRequiredError,
    BlockedError,
    ConfigError,
    ForbiddenOperationError,
    GitError,
    ManifestError,
)
from release_orchestrator.lab import INVOICE_DIR, LIBRARY_DIR, PAYMENT_DIR, build_lab
from release_orchestrator.models import Verdict
from release_orchestrator.planning.manifest import load_plan
from release_orchestrator.state import RunStatus
from release_orchestrator.versioning.semver import SemVer
from release_orchestrator.workflow import Orchestrator, RequestInput, Services, write_plan
from tests.conftest import snapshot_repo

pytestmark = pytest.mark.integration


class Lab:
    def __init__(self, root: Path, config_path: Path) -> None:
        self.root = root
        self.config_path = config_path
        self.reload()

    def reload(self) -> None:
        self.config = load_config(self.config_path)
        self.orch = Orchestrator(Services.from_config(self.config))

    @property
    def lib(self) -> Path:
        return self.root / LIBRARY_DIR

    def git(self, repo: str | Path, *args: str) -> str:
        path = repo if isinstance(repo, Path) else self.root / repo
        return subprocess.run(
            ["git", "-C", str(path), *args], capture_output=True, text=True, check=True
        ).stdout.strip()

    def plan(self, *tickets: str, **kw: str):  # type: ignore[no-untyped-def]
        return self.orch.plan(RequestInput(tickets=tickets, **kw))[0]

    def files(self, ref: str) -> list[str]:
        return self.git(LIBRARY_DIR, "ls-tree", "-r", "--name-only", ref).split()

    def show(self, ref: str, path: str) -> str:
        return self.git(LIBRARY_DIR, "show", f"{ref}:{path}")

    def release(self, plan, *, pr: bool = True):  # type: ignore[no-untyped-def]
        self.orch.prepare(plan, approve=True, dry_run=False)
        self.orch.validate(plan)
        if pr:
            self.orch.open_pr(plan, approve=True)
        return self.orch.publish(plan, approve=True)

    def tag_shas(self) -> str:
        return self.git(LIBRARY_DIR, "show-ref", "--tags")


@pytest.fixture
def lab(tmp_path: Path) -> Lab:
    root = tmp_path / "lab"
    return Lab(root, build_lab(root))


def _ledger(lab: Lab) -> EnvironmentLedger:
    return EnvironmentLedger(lab.config, lab.orch.s.registry)


# ----------------------------------------------------------------------------- S01
def test_s01_normal_release(lab):
    plan = lab.plan("FIN-198")
    assert plan.status == "PLAN_READY" and plan.analysis["verdict"] == Verdict.INDEPENDENT
    assert lab.orch.prepare(plan, approve=True, dry_run=False)["status"] == "prepared"
    assert lab.orch.validate(plan)["status"] == "PASSED"
    pr = lab.orch.open_pr(plan, approve=True)
    assert pr["created"] is True and pr["state"] == "open-draft"
    files = lab.files("release/1.1.0")
    assert "src/zaidlibrary/currency.py" in files
    assert "total_with_tax" not in lab.show("release/1.1.0", "src/zaidlibrary/payments.py")


# ----------------------------------------------------------------------------- S02
def test_s02_independent_out_of_order_release_excludes_a(lab):
    plan = lab.plan("FIN-204")
    assert plan.blockers == []
    assert plan.analysis["verdict"] in (Verdict.INDEPENDENT, Verdict.INDEPENDENT_WITH_RISKS)
    assert "FIN-198" in [c.key for c in plan.excluded]
    lab.orch.prepare(plan, approve=True, dry_run=False)
    assert lab.orch.validate(plan)["status"] == "PASSED"
    assert "src/zaidlibrary/currency.py" not in lab.files("release/1.1.0")
    assert "total_with_tax" in lab.show("release/1.1.0", "src/zaidlibrary/payments.py")
    subjects = lab.git(LIBRARY_DIR, "log", "--format=%s", "v1.0.0..release/1.1.0")
    assert "FIN-198" not in subjects and "FIN-204" in subjects


# ----------------------------------------------------------------------------- S03
def test_s03_dependent_release_blocked_with_prerequisite(lab):
    plan = lab.plan("FIN-211")
    assert plan.status == "BLOCKED" and plan.analysis["verdict"] == Verdict.DEPENDENT
    reasons = {(b["reason"], b["depends_on"]) for b in plan.blockers}
    # Two independent sources agree: the code (static analysis) and the issue tracker.
    assert reasons == {("MISSING_PREREQUISITE", "FIN-198"), ("UNRESOLVED_BLOCKER", "FIN-198")}
    assert plan.analysis["probe"]["applied_cleanly"] is True  # Git alone would accept it
    option = next(r for r in plan.remediation if r["option"] == "include_prerequisites")
    assert option["changes"] == ["FIN-198", "FIN-211"]
    with pytest.raises(BlockedError):
        lab.orch.prepare(plan, approve=True, dry_run=False)
    # The remediation is a valid, testable release.
    fixed = lab.plan("FIN-198", "FIN-211")
    assert fixed.status == "PLAN_READY"
    lab.orch.prepare(fixed, approve=True, dry_run=False)
    assert lab.orch.validate(fixed)["status"] == "PASSED"


# ----------------------------------------------------------------------------- S04
def test_s04_conflict_detected_protected_refs_untouched(lab):
    before = snapshot_repo(lab.lib)
    plan = lab.plan("FIN-219")
    assert plan.analysis["verdict"] == Verdict.CONFLICT
    conflict = next(f for f in plan.analysis["findings"] if f["kind"] == "GIT_CONFLICT")
    assert conflict["evidence"]["conflicted_files"] == ["src/zaidlibrary/payments.py"]
    assert "FIN-215" in conflict["evidence"]["suspected_causes"]
    assert snapshot_repo(lab.lib) == before


# ----------------------------------------------------------------------------- S05
def test_s05_semantic_dependency_caught_by_validation(lab):
    plan = lab.plan("FIN-223")
    assert plan.status == "PLAN_READY"  # static analysis can only warn
    assert plan.analysis["probe"]["applied_cleanly"] is True
    warning = next(f for f in plan.analysis["findings"] if f["kind"] == "SHARED_SYMBOL_MODIFIED")
    assert warning["depends_on"] in ("FIN-215", "FIN-219")
    lab.orch.prepare(plan, approve=True, dry_run=False)
    with pytest.raises(BlockedError) as exc:
        lab.orch.validate(plan)
    assert exc.value.reason == "VALIDATION_FAILED"
    assert "1.13" in exc.value.details["results"][0]["output_tail"]
    assert lab.orch.status(plan).state is RunStatus.BLOCKED


# ----------------------------------------------------------------------------- S06
def test_s06_cumulative_rc_subset_isolation(lab):
    rc_tags_before = lab.tag_shas()
    blocked = lab.plan("FIN-204", "FIN-211", source="v1.1.0-rc.23", prerelease="rc")
    assert blocked.status == "BLOCKED"
    assert {b["depends_on"] for b in blocked.blockers} == {"FIN-198"}

    plan = lab.plan("FIN-204", source="v1.1.0-rc.23", prerelease="rc")
    assert plan.proposed_version == "1.1.0-rc.24"
    assert plan.status == "PLAN_READY"
    assert sorted(c.key for c in plan.excluded) == ["FIN-198", "FIN-211"]
    lab.release(plan, pr=False)
    files = lab.files("v1.1.0-rc.24")
    assert "src/zaidlibrary/currency.py" not in files
    assert "format_invoice_in" not in lab.show("v1.1.0-rc.24", "src/zaidlibrary/invoices.py")
    # rc.22 / rc.23 were never moved; rc.24 contains LESS than rc.23.
    assert all(line in lab.tag_shas() for line in rc_tags_before.splitlines())


# ----------------------------------------------------------------------------- S07
def test_s07_only_affected_consumer_updated(lab):
    plan = lab.plan("FIN-204")
    actions = {c["name"]: c["action"] for c in plan.consumers}
    assert actions == {"payment-service": "update", "invoice-service": "not_targeted"}
    invoice_before = snapshot_repo(lab.root / INVOICE_DIR)
    lab.release(plan)
    summary = lab.orch.update_consumers(plan, approve=True, dry_run=False)
    [update] = summary["updated"]
    assert update["consumer"] == "payment-service" and update["validation"]["status"] == "PASSED"
    pinned = lab.git(PAYMENT_DIR, "show", "deps/zaidlibrary-1.1.0:requirements.txt")
    assert pinned == "zaidlibrary==1.1.0"
    assert snapshot_repo(lab.root / INVOICE_DIR) == invoice_before


# ----------------------------------------------------------------------------- S08
def test_s08_failed_validation_blocks_pr_publish_and_consumers(lab):
    plan = lab.plan("FIN-223")
    lab.orch.prepare(plan, approve=True, dry_run=False)
    with pytest.raises(BlockedError):
        lab.orch.validate(plan)
    for step in (lab.orch.open_pr, lab.orch.publish, lab.orch.push):
        with pytest.raises(BlockedError) as exc:
            step(plan, approve=True)
        assert exc.value.reason == "NOT_VALIDATED"
    with pytest.raises(BlockedError) as exc:
        lab.orch.update_consumers(plan, approve=True, dry_run=False)
    assert exc.value.reason == "VERSION_NOT_PUBLISHED"
    assert lab.orch.s.registry.list_versions("zaidlibrary") == []
    assert "v1.1.0" not in lab.git(LIBRARY_DIR, "tag", "--list", "v1.1.0")


# ----------------------------------------------------------------------------- S09
def test_s09_idempotent_requests(lab, tmp_path):
    first, second = lab.plan("FIN-204"), lab.plan("FIN-204")
    assert first.plan_id == second.plan_id and first.to_json() == second.to_json()
    bare = _bare_remote(tmp_path, lab, LIBRARY_DIR)
    for _ in range(2):
        lab.orch.prepare(first, approve=True, dry_run=False)
        lab.orch.validate(first)
        pr = lab.orch.open_pr(first, approve=True)
        published = lab.orch.publish(first, approve=True)
        pushed = lab.orch.push(first, approve=True)["pushed"]
    assert pr["created"] is False and published["status"] == "already_published"
    assert {p["status"] for p in pushed} == {"already_pushed"}
    assert lab.git(LIBRARY_DIR, "branch", "--list", "release/*").count("release/") == 1
    assert lab.git(LIBRARY_DIR, "tag", "--list", "v1.1.0") == "v1.1.0"
    assert len(lab.orch.s.prs.list()) == 1  # type: ignore[attr-defined]
    assert len(list((lab.config.registry_path / "zaidlibrary").glob("*.json"))) == 1
    remote_refs = subprocess.run(
        ["git", "-C", str(bare), "show-ref"], capture_output=True, text=True, check=True
    ).stdout
    assert "refs/heads/release/1.1.0" in remote_refs and "refs/tags/v1.1.0" in remote_refs


# ----------------------------------------------------------------------------- S10
def test_s10_concurrent_requests_cannot_both_publish_1_1_0(lab):
    plan_a, plan_b = lab.plan("FIN-198"), lab.plan("FIN-204")  # planned "at the same time"
    assert plan_a.proposed_version == plan_b.proposed_version == "1.1.0"
    lab.release(plan_a)
    with pytest.raises(BlockedError) as exc:
        lab.orch.prepare(plan_b, approve=True, dry_run=False)
    assert exc.value.reason == "BRANCH_EXISTS_MISMATCH"
    assert (
        lab.orch.s.registry.get("zaidlibrary", SemVer.parse("1.1.0")).metadata["plan_id"]
        == plan_a.plan_id
    )
    replanned = lab.plan("FIN-204")
    assert replanned.base_ref == "v1.1.0" and replanned.proposed_version == "1.2.0"
    assert "FIN-198" in replanned.already_released


# ----------------------------------------------------------------------------- S11
def test_s11_breaking_api_major_and_consumer_protection(lab):
    plan = lab.plan("FIN-230")
    assert plan.proposed_version == "2.0.0" and plan.version["bump"] == "major"
    assert plan.analysis["public_api_removals"] == ["zaidlibrary.invoices.invoice_due_date"]
    actions = {c["name"]: c["action"] for c in plan.consumers}
    assert actions == {"payment-service": "not_targeted", "invoice-service": "update"}
    assert any("Major version" in r for r in plan.compatibility_risks)
    lab.release(plan)
    with pytest.raises(BlockedError) as exc:
        lab.orch.update_consumers(plan, approve=True, dry_run=False)
    assert exc.value.reason == "CONSUMER_VALIDATION_FAILED"
    assert exc.value.details["failed"] == ["invoice-service"]
    assert "deps/" not in lab.git(INVOICE_DIR, "branch")


# ----------------------------------------------------------------------------- S12
def test_s12_recovery_after_partial_publish_failure(lab):
    plan = lab.plan("FIN-204")
    lab.orch.prepare(plan, approve=True, dry_run=False)
    lab.orch.validate(plan)
    lock = lab.lib / ".git" / "refs" / "tags" / "v1.1.0.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("simulated concurrent writer")  # tag creation will fail
    with pytest.raises(GitError):
        lab.orch.publish(plan, approve=True)
    assert lab.orch.status(plan).state is RunStatus.FAILED
    assert lab.orch.s.registry.list_versions("zaidlibrary") == [SemVer.parse("1.1.0")]
    lock.unlink()
    result = lab.orch.publish(plan, approve=True)
    assert result["status"] == "already_published"  # artifact reused, not duplicated
    assert lab.git(LIBRARY_DIR, "rev-parse", "v1.1.0") == lab.git(
        LIBRARY_DIR, "rev-parse", "release/1.1.0"
    )
    assert lab.orch.status(plan).state is RunStatus.PUBLISHED


# ----------------------------------------------------------------------------- S13
def test_s13_environment_promotion_rules(lab):
    lab.release(lab.plan("FIN-204"), pr=False)
    ledger = _ledger(lab)
    with pytest.raises(BlockedError) as exc:
        ledger.promote("1.1.0", "stage", approved_by=["alice"], approve=True)
    assert exc.value.reason == "PROMOTION_ORDER"
    with pytest.raises(ApprovalRequiredError):
        ledger.promote("1.1.0", "prep", approved_by=["alice"], approve=False)
    assert (
        ledger.promote("1.1.0", "prep", approved_by=["alice"], approve=True)["status"] == "promoted"
    )
    assert ledger.promote("1.1.0", "prep", approved_by=["alice"], approve=True)["status"] == (
        "already_current"
    )
    ledger.promote("1.1.0", "stage", approved_by=["alice"], approve=True)
    with pytest.raises(BlockedError) as exc:
        ledger.promote("1.1.0", "master", approved_by=["alice", "alice"], approve=True)
    assert exc.value.reason == "INSUFFICIENT_APPROVALS"
    ledger.promote("1.1.0", "master", approved_by=["alice", "bob"], approve=True)
    assert ledger.status()["master"]["current"] == "1.1.0"
    with pytest.raises(BlockedError) as exc:
        ledger.promote("9.9.9", "prep", approved_by=["alice"], approve=True)
    assert exc.value.reason == "VERSION_NOT_PUBLISHED"
    # A release candidate may reach stage but never master.
    rc = lab.plan("FIN-198", prerelease="rc")
    lab.release(rc, pr=False)
    for env in ("prep", "stage"):
        ledger.promote(rc.proposed_version, env, approved_by=["alice"], approve=True)
    with pytest.raises(BlockedError) as exc:
        ledger.promote(rc.proposed_version, "master", approved_by=["alice", "bob"], approve=True)
    assert exc.value.reason == "PRERELEASE_NOT_ALLOWED"


# ----------------------------------------------------------------------------- S14
def test_s14_rollback_is_roll_forward_without_rewriting_history(lab):
    lab.release(lab.plan("FIN-204"), pr=False)  # 1.1.0
    lab.release(lab.plan("FIN-198"), pr=False)  # 1.2.0, based on v1.1.0
    ledger = _ledger(lab)
    for version in ("1.1.0", "1.2.0"):
        for env in ("prep", "stage"):
            ledger.promote(version, env, approved_by=["alice"], approve=True)
    tags_before = lab.tag_shas()
    plan = ledger.rollback_plan("stage")
    assert plan["from_version"] == "1.2.0" and plan["to_version"] == "1.1.0"
    assert plan["artifact_verified"] is True
    with pytest.raises(ApprovalRequiredError):
        ledger.rollback("stage", approved_by=["alice"], approve=False)
    result = ledger.rollback("stage", approved_by=["alice"], approve=True)
    assert result["record"]["kind"] == "rollback" and ledger.current("stage") == "1.1.0"
    assert [r["version"] for r in ledger.history("stage")] == ["1.1.0", "1.2.0", "1.1.0"]
    assert lab.tag_shas() == tags_before
    assert lab.orch.s.registry.list_versions("zaidlibrary") == [
        SemVer.parse("1.1.0"),
        SemVer.parse("1.2.0"),
    ]


# ----------------------------------------------------------------------------- S15
def test_s15_ambiguous_mapping_rejected(lab):
    (lab.lib / "README.md").write_text("touched by two tickets\n")
    lab.git(
        LIBRARY_DIR,
        "-c",
        "user.name=x",
        "-c",
        "user.email=x@example.invalid",
        "commit",
        "-qam",
        "FIN-204 FIN-211: shared tweak",
    )
    plan = lab.plan("FIN-204")
    assert plan.status == "BLOCKED"
    assert "AMBIGUOUS_COMMIT" in {b["reason"] for b in plan.blockers}


def test_s15_malicious_ticket_text_is_inert(lab):
    issue = lab.orch.s.tracker.get("FIN-230")
    assert issue.consumers == ()  # "payment-service; rm -rf /" was dropped
    plan = lab.plan("FIN-230")
    text = plan.to_json()
    assert "curl" not in text and "rm -rf" not in text
    assert all("curl" not in " ".join(r["argv"]) for r in plan.validation_required)


def test_s15_unsafe_validation_command_rejected(tmp_path):
    root = tmp_path / "lab"
    config = build_lab(root)
    config.write_text(config.read_text().replace("{python}", "{ticket_description}", 1))
    with pytest.raises(ConfigError):
        load_config(config)


def test_s15_unapproved_writes_rejected(lab):
    plan = lab.plan("FIN-204")
    with pytest.raises(ApprovalRequiredError):
        lab.orch.prepare(plan, approve=False, dry_run=False)
    lab.orch.prepare(plan, approve=True, dry_run=False)
    lab.orch.validate(plan)
    for step in (lab.orch.open_pr, lab.orch.publish, lab.orch.push):
        with pytest.raises(ApprovalRequiredError):
            step(plan, approve=False)


def test_s15_tampered_manifest_rejected(lab, tmp_path):
    path, _ = write_plan(lab.plan("FIN-204"), tmp_path / "plan.json")
    data = json.loads(path.read_text())
    data["included_changes"][0]["commits"][0]["sha"] = "0" * 40
    path.write_text(json.dumps(data))
    with pytest.raises(ManifestError):
        load_plan(path, lab.config)


def test_s15_push_only_to_allowlisted_remotes(lab):
    plan = lab.plan("FIN-204")
    lab.orch.prepare(plan, approve=True, dry_run=False)
    lab.orch.validate(plan)
    lab.git(LIBRARY_DIR, "remote", "add", "origin", "https://github.com/attacker/stolen.git")
    with pytest.raises(ForbiddenOperationError):
        lab.orch.push(plan, approve=True)
    with pytest.raises(ForbiddenOperationError):
        lab.orch.s.git.run("push", "origin", "+refs/heads/main:refs/heads/main", allow_push=True)
    with pytest.raises(ForbiddenOperationError):
        lab.orch.s.git.run("push", "--force", "origin", "a:b", allow_push=True)


def test_s15_external_pr_record_rejects_foreign_urls(tmp_path):
    from release_orchestrator.integrations.pull_requests import (
        ExternalPullRequestProvider,
        PullRequestSpec,
    )

    provider = ExternalPullRequestProvider(tmp_path)
    ref = provider.ensure(PullRequestSpec("zaidlibrary", "release/1.1.0", "main", "t", "b"))
    assert ref.state == "awaiting_external_creation"
    with pytest.raises(ValueError):
        provider.record(
            "zaidlibrary",
            "release/1.1.0",
            number=1,
            url="https://evil.example/pull/1",
            state="open-draft",
        )
    provider.record(
        "zaidlibrary",
        "release/1.1.0",
        number=3,
        url="https://github.com/zaidmoen/harri-release-lab-library/pull/3",
        state="open-draft",
    )
    again = provider.ensure(PullRequestSpec("zaidlibrary", "release/1.1.0", "main", "t", "b"))
    assert again.identifier == "3" and again.created is False


# ----------------------------------------------------------------------------- helpers
def _bare_remote(tmp_path: Path, lab: Lab, repo_dir: str) -> Path:
    """A local bare 'origin' that the config allowlists (file://), so pushes are real."""
    bare = tmp_path / f"{repo_dir}.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    url = bare.as_uri()
    lab.git(repo_dir, "remote", "add", "origin", url)
    config = lab.config_path.read_text()
    config = config.replace("remote_allowlist:\n", f"remote_allowlist:\n  - {url}\n", 1)
    lab.config_path.write_text(config)
    lab.reload()
    return bare


# ----------------------------------------------------------------------------- live-run regressions
def test_release_pr_targets_integration_branch_at_release_base(lab, tmp_path):
    """ADR-0009: the release PR must show exactly the release, never target the trunk."""
    plan = lab.plan("FIN-204")
    lab.orch.prepare(plan, approve=True, dry_run=False)
    lab.orch.validate(plan)
    pr = lab.orch.open_pr(plan, approve=True)
    assert pr["base"] == "release-base/v1.0.0"
    assert lab.git(LIBRARY_DIR, "rev-parse", "release-base/v1.0.0") == plan.base_revision
    assert lab.git(LIBRARY_DIR, "rev-parse", "main") != plan.base_revision  # trunk untouched
    bare = _bare_remote(tmp_path, lab, LIBRARY_DIR)
    refs = {p["ref"] for p in lab.orch.push(plan, approve=True)["pushed"]}
    assert "refs/heads/release-base/v1.0.0" in refs
    remote = subprocess.run(
        ["git", "-C", str(bare), "rev-parse", "release-base/v1.0.0"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert remote == plan.base_revision


def test_existing_integration_branch_elsewhere_is_never_moved(lab):
    plan = lab.plan("FIN-204")
    lab.orch.prepare(plan, approve=True, dry_run=False)
    lab.orch.validate(plan)
    lab.git(LIBRARY_DIR, "branch", "release-base/v1.0.0", "main")
    with pytest.raises(BlockedError) as exc:
        lab.orch.open_pr(plan, approve=True)
    assert exc.value.reason == "INTEGRATION_BRANCH_MISMATCH"
    assert lab.git(LIBRARY_DIR, "rev-parse", "release-base/v1.0.0") == lab.git(
        LIBRARY_DIR, "rev-parse", "main"
    )


def test_replan_after_release_is_recognised_not_blocked(lab, capsys):
    from release_orchestrator.cli import main

    plan = lab.plan("FIN-204")
    lab.release(plan)
    code = main(["--json", "--config", str(lab.config_path), "plan", "--ticket", "FIN-204"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["result"]["status"] == "ALREADY_RELEASED"
    assert out["result"]["released_in"] == "v1.1.0"
    assert out["result"]["source_commits"]["FIN-204"] == plan.included_shas


def test_unsubmitted_external_pr_request_can_be_corrected(tmp_path):
    from release_orchestrator.integrations.pull_requests import (
        ExternalPullRequestProvider,
        PullRequestSpec,
    )

    provider = ExternalPullRequestProvider(tmp_path)
    provider.ensure(PullRequestSpec("lib", "release/1.1.0", "main", "t", "b"))
    ref = provider.ensure(PullRequestSpec("lib", "release/1.1.0", "release-base/v1.0.0", "t", "b"))
    assert ref.base == "release-base/v1.0.0"
    provider.record(
        "lib",
        "release/1.1.0",
        number=4,
        url="https://github.com/zaidmoen/harri-release-lab-library/pull/4",
        state="open-draft",
    )
    later = provider.ensure(PullRequestSpec("lib", "release/1.1.0", "main", "t", "b"))
    assert later.base == "release-base/v1.0.0"  # a created PR is never silently retargeted
