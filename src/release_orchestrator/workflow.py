"""Workflow service layer: composes the engines, enforces approval gates and run states.

This is the only module that knows about all components. Each public method is one CLI
step, is idempotent, and records its outcome in the run state and report files.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, NoReturn, TypeVar

from release_orchestrator.analysis.dependency import AnalysisResult, DependencyAnalyzer
from release_orchestrator.config import Config
from release_orchestrator.consumers.updater import prepare_consumer_update
from release_orchestrator.errors import (
    ApprovalRequiredError,
    BlockedError,
    ImmutableVersionError,
    InvalidInputError,
    OrchestratorError,
)
from release_orchestrator.fsutil import temp_dir
from release_orchestrator.gitutil.history import HistoryScan, scan_history
from release_orchestrator.gitutil.repo import Git
from release_orchestrator.integrations.github import GitHubPullRequestProvider
from release_orchestrator.integrations.github_status import (
    ApprovalPolicy,
    GitHubReader,
    evaluate_approval,
)
from release_orchestrator.integrations.jira import (
    FixtureIssueTracker,
    IssueTracker,
    NullIssueTracker,
)
from release_orchestrator.integrations.pr_reconcile import reconcile
from release_orchestrator.integrations.pull_requests import (
    ExternalPullRequestProvider,
    LocalPullRequestProvider,
    PullRequestProvider,
    PullRequestSpec,
)
from release_orchestrator.isolation.release_branch import prepare_release
from release_orchestrator.planning.manifest import verify_release_branch
from release_orchestrator.planning.plan import ReleasePlan
from release_orchestrator.planning.planner import build_plan, resolve_base
from release_orchestrator.reporting.markdown import (
    consumer_pr_body,
    library_pr_body,
    render_plan,
    render_run_report,
)
from release_orchestrator.requests import (
    ResolvedRequest,
    resolve_commits,
    resolve_text,
    resolve_ticket,
)
from release_orchestrator.state import (
    PREPARED_STATES,
    TRANSITIONS,
    VALIDATED_STATES,
    RunState,
    RunStatus,
    RunStore,
)
from release_orchestrator.validation.runner import run_validation
from release_orchestrator.versioning.pypi import build_wheel, extract_artifact
from release_orchestrator.versioning.registry import (
    PackageRegistry,
    PublishedArtifact,
    build_registry,
)
from release_orchestrator.versioning.semver import SemVer

log = logging.getLogger(__name__)
T = TypeVar("T")


@dataclass
class RequestInput:
    tickets: tuple[str, ...] = ()
    text: str | None = None
    commits: tuple[str, ...] = ()
    base: str | None = None
    source: str | None = None
    prerelease: str | None = None


@dataclass
class Services:
    config: Config
    git: Git
    tracker: IssueTracker
    registry: PackageRegistry
    prs: PullRequestProvider
    runs: RunStore
    github: GitHubReader = field(default_factory=GitHubReader)

    @classmethod
    def from_config(cls, config: Config) -> Services:
        git = Git(config.library.repo)
        if not git.is_repo():
            raise InvalidInputError(f"Library repo is not a Git repository: {config.library.repo}")
        tracker: IssueTracker = (
            FixtureIssueTracker(config.issue_tracker_fixture)
            if config.issue_tracker_fixture
            else NullIssueTracker()
        )
        prs: PullRequestProvider
        if config.pr_provider == "github":
            prs = GitHubPullRequestProvider(
                config.github_repositories, api_url=config.github_api_url
            )
        elif config.pr_provider == "external":
            prs = ExternalPullRequestProvider(config.state_dir / "pr-requests")
        else:
            prs = LocalPullRequestProvider(config.state_dir / "pull-requests")
        return cls(
            config,
            git,
            tracker,
            build_registry(config),
            prs,
            RunStore(config.state_dir),
            GitHubReader(api_url=config.github_api_url),
        )


class Orchestrator:
    def __init__(self, services: Services) -> None:
        self.s = services
        self.config = services.config

    # ------------------------------------------------------------------ read-only
    def scan(self, request: RequestInput) -> HistoryScan:
        base = resolve_base(self.config, self.s.git, request.base)
        titles = {}
        for key in self.s.tracker.keys():  # noqa: SIM118 - tracker API, not a dict
            issue = self.s.tracker.get(key)
            if issue is not None:
                titles[key] = issue.summary
        return scan_history(
            self.s.git,
            base_ref=base,
            source_ref=request.source or self.config.library.source_ref,
            key_pattern=self.config.change_key_pattern,
            titles=titles,
        )

    def resolve(self, request: RequestInput, scan: HistoryScan) -> ResolvedRequest:
        given = sum(bool(x) for x in (request.tickets, request.text, request.commits))
        if given != 1:
            raise InvalidInputError("Provide exactly one of --ticket, --request or --commit")
        pattern = self.config.change_key_pattern
        if request.text:
            return resolve_text(
                request.text,
                key_pattern=pattern,
                aliases=self.config.aliases,
                tracker=self.s.tracker,
            )
        if request.commits:
            return resolve_commits(list(request.commits), scan)
        resolved = [
            resolve_ticket(t, key_pattern=pattern, tracker=self.s.tracker) for t in request.tickets
        ]
        keys = tuple(dict.fromkeys(k for r in resolved for k in r.change_keys))
        notes = tuple(dict.fromkeys(n for r in resolved for n in r.notes))
        return ResolvedRequest("ticket", " ".join(request.tickets), keys, notes=notes)

    def analyze(self, request: RequestInput) -> tuple[ResolvedRequest, HistoryScan, AnalysisResult]:
        scan = self.scan(request)
        resolved = self.resolve(request, scan)
        pending = [k for k in resolved.change_keys if k in scan.changes]
        missing = [k for k in resolved.change_keys if k not in scan.changes]
        if not pending:
            raise BlockedError(
                "None of the requested changes has pending commits",
                reason="NOTHING_TO_RELEASE",
                details={"keys": missing, "already_released": scan.already_released},
            )
        analysis = DependencyAnalyzer(self.s.git, self.config.library.package_dir).analyze(
            scan, pending
        )
        return resolved, scan, analysis

    def plan(self, request: RequestInput) -> tuple[ReleasePlan, RunState]:
        scan = self.scan(request)
        resolved = self.resolve(request, scan)
        plan, _ = build_plan(
            config=self.config,
            git=self.s.git,
            scan=scan,
            request=resolved,
            tracker=self.s.tracker,
            registry=self.s.registry,
            prerelease=request.prerelease,
        )
        run, created = self.s.runs.create_or_load(plan.plan_id, plan.library, plan.proposed_version)
        if created:
            target = RunStatus.PLAN_READY if plan.status == "PLAN_READY" else RunStatus.BLOCKED
            run.transition(
                target,
                "plan generated"
                if target is RunStatus.PLAN_READY
                else "; ".join(b["reason"] for b in plan.blockers),
            )
            run.record("plan", {"status": plan.status, "verdict": plan.analysis["verdict"]})
        self._save(plan, run)
        return plan, run

    def verify(self, plan: ReleasePlan) -> dict[str, Any]:
        return verify_release_branch(self.s.git, plan).to_dict()

    def status(self, plan: ReleasePlan) -> RunState:
        return self._run(plan)

    # ------------------------------------------------------------------ write steps
    def prepare(self, plan: ReleasePlan, *, approve: bool, dry_run: bool) -> dict[str, Any]:
        run = self._run(plan)
        if dry_run:
            return prepare_release(
                self.s.git, plan, dry_run=True, version_pattern=self.config.library.version_pattern
            ).to_dict()
        if run.state in PREPARED_STATES:
            report = verify_release_branch(self.s.git, plan)
            if not report.ok:
                raise BlockedError(
                    "Release branch no longer matches the plan",
                    reason="MANIFEST_MISMATCH",
                    details=report.to_dict(),
                )
            return {
                "status": "already_prepared",
                "branch": plan.branch,
                "head": report.head,
                "provenance": report.provenance,
                "state": str(run.state),
            }
        if plan.status != "PLAN_READY":
            raise BlockedError(
                "Plan is BLOCKED; it cannot be prepared",
                reason="PLAN_BLOCKED",
                details={"blockers": plan.blockers},
            )
        self._require_approval(run, plan, approve, "prepare")
        run.transition(RunStatus.PREPARING, "approved prepare")
        result = self._guarded(
            run,
            plan,
            lambda: prepare_release(
                self.s.git, plan, dry_run=False, version_pattern=self.config.library.version_pattern
            ),
        )
        run.transition(RunStatus.VALIDATING, f"{result.status} {result.branch}")
        run.record("prepare", result.to_dict())
        self._save(plan, run)
        return result.to_dict()

    def validate(self, plan: ReleasePlan) -> dict[str, Any]:
        run = self._run(plan)
        if run.state not in PREPARED_STATES and "prepare" not in run.steps:
            raise BlockedError(
                "Release branch has not been prepared",
                reason="NOT_PREPARED",
                details={"state": str(run.state)},
            )
        report = verify_release_branch(self.s.git, plan)
        if run.state in (RunStatus.BLOCKED, RunStatus.FAILED):
            run.transition(RunStatus.VALIDATING, "re-validation")
        elif run.state not in PREPARED_STATES:
            raise BlockedError(
                "Release branch has not been prepared",
                reason="NOT_PREPARED",
                details={"state": str(run.state)},
            )
        if not report.ok:
            self._block(
                run,
                plan,
                BlockedError(
                    "Release branch does not match the approved plan",
                    reason="MANIFEST_MISMATCH",
                    details=report.to_dict(),
                ),
            )
        if not self.config.library_validation:
            self._block(
                run,
                plan,
                BlockedError(
                    "No validation commands configured", reason="NO_VALIDATION_CONFIGURED"
                ),
            )
        with temp_dir("lro-validate-") as tmp:
            self.s.git.archive_to(str(report.head), tmp)
            validation = run_validation(
                self.config.library_validation, scope="library", cwd=tmp, library_path=tmp
            )
        result = {"head": report.head, **validation.to_dict()}
        run.record("validate", result)
        if not validation.passed:
            run.transition(RunStatus.BLOCKED, "validation failed")
            self._save(plan, run)
            raise BlockedError(
                "Validation failed; the release is not ready",
                reason="VALIDATION_FAILED",
                details=result,
            )
        if run.state is RunStatus.VALIDATING:
            run.transition(RunStatus.PR_READY, "verified and validated")
        self._save(plan, run)
        return result

    def open_pr(self, plan: ReleasePlan, *, approve: bool) -> dict[str, Any]:
        run = self._run(plan)
        if not self._validated(run):
            raise BlockedError(
                "Validation has not passed; refusing to prepare a PR",
                reason="NOT_VALIDATED",
                details={"state": str(run.state)},
            )
        self._require_approval(run, plan, approve, "pr")
        validation = run.steps.get("validate", {}).get("result", {})
        spec = PullRequestSpec(
            repository=self.config.library.name,
            head=plan.branch,
            base=self._pr_base(run, plan),
            title=(
                f"Release {plan.library} {plan.proposed_version} ({', '.join(plan.included_keys)})"
            ),
            body=library_pr_body(plan, validation),
        )
        ref = self._guarded(run, plan, lambda: self.s.prs.ensure(spec)).to_dict()
        run.record("pr", ref)
        self._save(plan, run)
        return ref

    def publish(self, plan: ReleasePlan, *, approve: bool) -> dict[str, Any]:
        run = self._run(plan)
        if not self._validated(run):
            raise BlockedError(
                "Only validated releases can be published",
                reason="NOT_VALIDATED",
                details={"state": str(run.state)},
            )
        self._require_approval(run, plan, approve, "publish")
        self._authorize_publication(run, plan)
        report = verify_release_branch(self.s.git, plan)
        if not report.ok:
            self._block(
                run,
                plan,
                BlockedError(
                    "Release branch does not match the plan",
                    reason="MANIFEST_MISMATCH",
                    details=report.to_dict(),
                ),
            )
        head = str(report.head)
        tag = f"{self.config.library.tag_prefix}{plan.proposed_version}"
        tagged = self.s.git.try_rev_parse(f"refs/tags/{tag}")
        if tagged is not None and tagged != head:
            self._block(
                run,
                plan,
                BlockedError(
                    f"Tag {tag} already points at another commit",
                    reason="VERSION_COLLISION",
                    details={"tag": tag, "tag_commit": tagged, "release_head": head},
                ),
            )
        version = SemVer.parse(plan.proposed_version)
        metadata = {
            "plan_id": plan.plan_id,
            "release_commit": head,
            "base_revision": plan.base_revision,
            "changes": plan.included_keys,
            "source_commits": plan.included_shas,
        }
        try:
            filename, payload = self._artifact(head, version)
            artifact, created = self.s.registry.publish(
                plan.library, version, payload, {**metadata, "filename": filename}
            )
        except ImmutableVersionError as exc:
            self._block(run, plan, exc)
        if tagged is None:
            # The artifact is already stored; if tagging fails the run is FAILED and a
            # re-run finds the artifact (same digest) and only creates the missing tag.
            self._guarded(run, plan, partial(self.s.git.run, "tag", tag, head))
        result = {
            "status": "published" if created else "already_published",
            "artifact": artifact.to_dict(),
            "tag": tag,
        }
        if run.state in (RunStatus.PR_READY, RunStatus.FAILED):
            run.transition(RunStatus.PUBLISHED, f"{plan.library} {version} published")
        run.record("publish", result)
        self._save(plan, run)
        return result

    def update_consumers(
        self, plan: ReleasePlan, *, approve: bool, dry_run: bool
    ) -> dict[str, Any]:
        run = self._run(plan)
        version = SemVer.parse(plan.proposed_version)
        artifact = self.s.registry.get(plan.library, version)
        if artifact is None:
            raise BlockedError(
                f"{plan.library} {version} is not published yet; consumer PRs would not be "
                "installable",
                reason="VERSION_NOT_PUBLISHED",
            )
        self._verify_artifact_identity(plan, artifact)
        if not dry_run:
            self._require_approval(run, plan, approve, "update-consumers")
        targets = [c for c in plan.consumers if c["action"] == "update"]
        results: list[dict[str, Any]] = []
        with temp_dir("lro-artifact-") as library_dir:
            extract_artifact(
                self.s.registry.artifact_kind,
                self.s.registry.fetch(plan.library, version),
                library_dir,
            )
            for target in targets:
                consumer = self.config.consumer(target["name"])
                update = self._guarded(
                    run,
                    plan,
                    partial(
                        prepare_consumer_update,
                        consumer,
                        library=plan.library,
                        from_version=target.get("current_version"),
                        to_version=plan.proposed_version,
                        change_keys=plan.included_keys,
                        library_dir=library_dir,
                        dry_run=dry_run,
                    ),
                ).to_dict()
                if (
                    not dry_run
                    and update["status"] in ("prepared", "already_prepared")
                    and update["validation"]
                    and update["validation"]["status"] == "PASSED"
                ):
                    spec = PullRequestSpec(
                        repository=consumer.name,
                        head=update["branch"],
                        base=consumer.source_ref,
                        title=f"Bump {plan.library} to {plan.proposed_version}",
                        body=consumer_pr_body(plan, consumer.name, update),
                    )
                    update["pull_request"] = self.s.prs.ensure(spec).to_dict()
                results.append(update)
        skipped = [
            {"consumer": c["name"], "action": c["action"], "reasons": c.get("reasons", [])}
            for c in plan.consumers
            if c["action"] != "update"
        ]
        failed = [r["consumer"] for r in results if r["status"] == "validation_failed"]
        summary = {"dry_run": dry_run, "updated": results, "not_updated": skipped, "failed": failed}
        if dry_run:
            return summary
        run.record("update-consumers", summary)
        if failed:
            if run.state is RunStatus.PUBLISHED:
                run.transition(RunStatus.BLOCKED, f"consumer validation failed: {failed}")
            self._save(plan, run)
            raise BlockedError(
                "Consumer validation failed", reason="CONSUMER_VALIDATION_FAILED", details=summary
            )
        if run.state is RunStatus.PUBLISHED:
            run.transition(RunStatus.COMPLETED, f"{len(results)} consumer update(s) prepared")
        self._save(plan, run)
        return summary

    def push(self, plan: ReleasePlan, *, approve: bool) -> dict[str, Any]:
        """Push the release branch (+ tag once published) and prepared consumer branches.

        Only to remotes in ``remote_allowlist``; one ref per push; never forced.
        """
        run = self._run(plan)
        if not self._validated(run):
            raise BlockedError(
                "Only verified and validated releases can be pushed",
                reason="NOT_VALIDATED",
                details={"state": str(run.state)},
            )
        if not self.config.remote_allowlist:
            raise BlockedError(
                "remote_allowlist is empty; nothing may be pushed", reason="NO_REMOTE_ALLOWLIST"
            )
        report = verify_release_branch(self.s.git, plan)
        if not report.ok:
            self._block(
                run,
                plan,
                BlockedError(
                    "Release branch does not match the plan",
                    reason="MANIFEST_MISMATCH",
                    details=report.to_dict(),
                ),
            )
        if not approve:
            raise ApprovalRequiredError(
                "'push' writes to remote repositories and needs --approve",
                details={"step": "push", "plan_id": plan.plan_id},
            )
        allow = self.config.remote_allowlist
        remote = self.config.remote_name
        pushed = [self.s.git.push_ref(remote, f"refs/heads/{plan.branch}", allowlist=allow)]
        integration = integration_branch(self.config, plan)
        if integration and self.s.git.branch_exists(integration):
            pushed.append(self.s.git.push_ref(remote, f"refs/heads/{integration}", allowlist=allow))
        tag = f"{self.config.library.tag_prefix}{plan.proposed_version}"
        if "publish" in run.steps and self.s.git.try_rev_parse(f"refs/tags/{tag}"):
            pushed.append(self.s.git.push_ref(remote, f"refs/tags/{tag}", allowlist=allow))
        consumers = run.steps.get("update-consumers", {}).get("result", {}).get("updated", [])
        for update in consumers:
            if update["status"] in ("prepared", "already_prepared"):
                git = Git(self.config.consumer(update["consumer"]).repo)
                pushed.append(
                    {
                        "consumer": update["consumer"],
                        **git.push_ref(remote, f"refs/heads/{update['branch']}", allowlist=allow),
                    }
                )
        run.record("push", pushed)
        self._save(plan, run)
        return {"pushed": pushed}

    def record_pr(
        self, plan: ReleasePlan, *, repository: str, head: str, number: int, url: str, state: str
    ) -> dict[str, Any]:
        if not isinstance(self.s.prs, ExternalPullRequestProvider):
            raise InvalidInputError("record-pr is only used with pull_requests.provider: external")
        run = self._run(plan)
        try:
            recorded = self.s.prs.record(repository, head, number=number, url=url, state=state)
        except (FileNotFoundError, ValueError) as exc:
            raise InvalidInputError(str(exc)) from exc
        prs = dict(run.steps.get("recorded-prs", {}).get("result", {}))
        prs[f"{repository}:{head}"] = {"number": number, "url": url, "state": state}
        run.record("recorded-prs", prs)
        self._save(plan, run)
        return recorded

    def reconcile_prs(self, plan: ReleasePlan) -> dict[str, Any]:
        """Compare recorded PR requests with the PRs GitHub actually has (ADR-0010).

        Read-only towards the remote. Locally it records PRs it discovers. Run it before
        creating any PR externally: a ``discovered`` or ``recorded_verified`` result means
        the PR already exists and must not be created again.
        """
        if not isinstance(self.s.prs, ExternalPullRequestProvider):
            raise InvalidInputError("reconcile-prs applies to pull_requests.provider: external")
        run = self._run(plan)
        remote = self.config.remote_name
        targets: list[tuple[str, str, Git]] = [(self.config.library.name, plan.branch, self.s.git)]
        consumers = run.steps.get("update-consumers", {}).get("result", {}).get("updated", [])
        for update in consumers:
            if update["status"] in ("prepared", "already_prepared"):
                repo = Git(self.config.consumer(update["consumer"]).repo)
                targets.append((update["consumer"], update["branch"], repo))
        results = [
            reconcile(self.s.prs, repository=name, head=head, git=git, remote=remote).to_dict()
            for name, head, git in targets
        ]
        summary = {"ok": all(r["ok"] for r in results), "pull_requests": results}
        run.record("reconcile-prs", summary)
        self._save(plan, run)
        return summary

    def pr_status(self, plan: ReleasePlan, run: RunState | None = None) -> dict[str, Any]:
        """Authoritative PR state from GitHub plus the approval decision (ADR-0011).

        ``run`` lets a caller that already holds the run state (``publish``) pass it in, so
        the evidence is recorded on the same object that is saved last (no lost update).
        """
        run = run if run is not None else self._run(plan)
        policy = self.config.approval_policy
        repository = self.config.github_repositories.get(self.config.library.name)
        if not repository:
            raise BlockedError(
                f"No GitHub repository mapped for '{self.config.library.name}' "
                "(pull_requests.github_repositories)",
                reason="GITHUB_REPO_UNMAPPED",
            )
        number = self._recorded_pr_number(plan)
        report = verify_release_branch(self.s.git, plan)
        if not report.ok or report.head is None:
            self._block(
                run,
                plan,
                BlockedError(
                    "Release branch does not match the plan",
                    reason="MANIFEST_MISMATCH",
                    details=report.to_dict(),
                ),
            )
        evidence = self.s.github.evidence(repository, number)
        result: dict[str, Any] = {"repository": repository, "evidence": evidence.to_dict()}
        if policy is not None:
            decision = evaluate_approval(
                evidence,
                ApprovalPolicy(
                    policy.required_approvals,
                    policy.required_checks,
                    policy.require_ready_for_review,
                ),
                expected_head=str(report.head),
            )
            result["policy"] = asdict(policy)
            result["decision"] = decision.to_dict()
        run.record("pr-status", result)
        self._save(plan, run)
        return result

    def _recorded_pr_number(self, plan: ReleasePlan) -> int:
        record = (
            self.s.prs.get(self.config.library.name, plan.branch)
            if isinstance(self.s.prs, ExternalPullRequestProvider)
            else None
        )
        if not record or record.get("number") is None:
            raise BlockedError(
                "No library PR is recorded for this release; run reconcile-prs (or record-pr) "
                "after the PR exists",
                reason="PR_NOT_RECORDED",
            )
        return int(record["number"])

    def _authorize_publication(self, run: RunState, plan: ReleasePlan) -> None:
        """Publishing needs more than the operator's --approve (ADR-0011).

        * ``approval_policy`` configured: a GitHub review approval on the current release
          commit, plus required checks, verified live. A draft or stale approval is not
          enough.
        * production registry without a policy: refused. Nobody may publish to production
          on the operator's word alone.
        """
        if self.config.approval_policy is not None:
            status = self.pr_status(plan, run)
            decision = status["decision"]
            if not decision["approved"]:
                self._block(
                    run,
                    plan,
                    BlockedError(
                        "Release approval is not verified on GitHub; publication refused",
                        reason="APPROVAL_NOT_VERIFIED",
                        details=decision,
                    ),
                )
        elif self.config.registry_production:
            self._block(
                run,
                plan,
                BlockedError(
                    "Publishing to a production registry requires approval_policy (verified "
                    "GitHub approvals); --approve alone is not sufficient",
                    reason="PRODUCTION_PUBLISH_UNAUTHORIZED",
                ),
            )

    # ------------------------------------------------------------------ helpers
    def _artifact(self, head: str, version: SemVer) -> tuple[str, bytes]:
        """The deterministic artifact for a release commit (same commit → same sha256)."""
        if self.s.registry.artifact_kind == "wheel":
            return build_wheel(self.s.git, head, expected_version=version)
        return f"{self.config.library.name}-{version}.tar", self.s.git.archive_bytes(head)

    def _verify_artifact_identity(self, plan: ReleasePlan, artifact: PublishedArtifact) -> None:
        """Is the published artifact *this* plan's release? Verified, never assumed."""
        plan_id = artifact.metadata.get("plan_id")
        if plan_id is not None:
            if plan_id != plan.plan_id:
                raise BlockedError(
                    f"Published {plan.library} {artifact.version} was produced by a different plan",
                    reason="ARTIFACT_PLAN_MISMATCH",
                    details={"artifact_plan": plan_id},
                )
            return
        # Index registries carry no provenance metadata: rebuild from the verified release
        # commit and compare digests (wheels are reproducible, ADR-0012).
        report = verify_release_branch(self.s.git, plan)
        if not report.ok or report.head is None:
            raise BlockedError(
                "Release branch does not match the plan",
                reason="MANIFEST_MISMATCH",
                details=report.to_dict(),
            )
        _, payload = self._artifact(report.head, SemVer.parse(plan.proposed_version))
        rebuilt = hashlib.sha256(payload).hexdigest()
        if rebuilt != artifact.sha256:
            raise BlockedError(
                f"Published {plan.library} {artifact.version} does not match a rebuild of the "
                "release commit",
                reason="ARTIFACT_PLAN_MISMATCH",
                details={"published_sha256": artifact.sha256, "rebuilt_sha256": rebuilt},
            )

    def _pr_base(self, run: RunState, plan: ReleasePlan) -> str:
        """Base branch of the library release PR (ADR-0009).

        ``integration``: a branch pinned at the release base, created here if missing, so
        the PR shows exactly the release delta and merging it never touches the trunk.
        """
        branch = integration_branch(self.config, plan)
        if branch is None:
            return self.config.library.source_ref
        existing = self.s.git.try_rev_parse(f"refs/heads/{branch}")
        if existing is None:
            self.s.git.create_branch(branch, plan.base_revision)
        elif existing != plan.base_revision:
            self._block(
                run,
                plan,
                BlockedError(
                    f"Integration branch '{branch}' exists at {existing[:10]}, not at the release "
                    f"base {plan.base_revision[:10]}; it was not moved",
                    reason="INTEGRATION_BRANCH_MISMATCH",
                    details={"branch": branch, "sha": existing, "expected": plan.base_revision},
                ),
            )
        return branch

    def _validated(self, run: RunState) -> bool:
        if run.state in VALIDATED_STATES:
            return True
        last = run.steps.get("validate", {}).get("result", {})
        return run.state is RunStatus.FAILED and last.get("status") == "PASSED"

    def _run(self, plan: ReleasePlan) -> RunState:
        run = self.s.runs.load(plan.plan_id)
        if run is None:
            raise BlockedError(
                "No run state for this plan; generate it with 'plan' first",
                reason="UNKNOWN_PLAN",
                details={"plan_id": plan.plan_id},
            )
        return run

    def _require_approval(self, run: RunState, plan: ReleasePlan, approve: bool, step: str) -> None:
        if approve:
            return
        if run.state is RunStatus.PLAN_READY:
            run.transition(RunStatus.APPROVAL_REQUIRED, f"{step} requested without approval")
            self._save(plan, run)
        raise ApprovalRequiredError(
            f"'{step}' writes to repositories and needs explicit human approval (--approve)",
            details={"step": step, "plan_id": plan.plan_id},
        )

    def _guarded(self, run: RunState, plan: ReleasePlan, action: Callable[[], T]) -> T:
        try:
            return action()
        except BlockedError as exc:
            self._block(run, plan, exc)
        except OrchestratorError as exc:
            if RunStatus.FAILED in TRANSITIONS[run.state]:
                run.transition(RunStatus.FAILED, exc.message[:200])
            run.record("last_error", exc.to_dict())
            self._save(plan, run)
            raise

    def _block(self, run: RunState, plan: ReleasePlan, exc: BlockedError) -> NoReturn:
        if RunStatus.BLOCKED in TRANSITIONS[run.state]:
            run.transition(RunStatus.BLOCKED, exc.reason)
        run.record("last_error", exc.to_dict())
        self._save(plan, run)
        raise exc

    def _save(self, plan: ReleasePlan, run: RunState) -> None:
        self.s.runs.save(run)
        reports = self.config.state_dir / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        (reports / f"{plan.plan_id}.json").write_text(
            json.dumps({"plan": plan.to_dict(), "run": run.to_dict()}, indent=2), encoding="utf-8"
        )
        (reports / f"{plan.plan_id}.md").write_text(
            render_run_report(plan, run.to_dict()), encoding="utf-8"
        )


def write_plan(plan: ReleasePlan, out: Path) -> tuple[Path, Path]:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(plan.to_json(), encoding="utf-8")
    md = out.with_suffix(".md")
    md.write_text(render_plan(plan), encoding="utf-8")
    return out, md


def integration_branch(config: Config, plan: ReleasePlan) -> str | None:
    """``release-base/<base_ref>`` when release PRs target an integration branch."""
    if config.library.release_pr_target != "integration":
        return None
    return f"release-base/{plan.base_ref}"
