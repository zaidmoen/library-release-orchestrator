"""Authoritative pull request state from the GitHub REST API (read-only, ADR-0011).

Git refs (ADR-0010) prove a PR exists and which commit it points at. Only the API knows
whether it is open, draft or merged, who reviewed which commit with what permission, and
how the checks concluded. This reader fetches exactly that, with:

* an optional token from ``GITHUB_TOKEN`` (public repositories work anonymously for PR,
  review and check data; reviewer permissions and branch protection need a token);
* bounded retries for transient failures (network, 5xx, rate limit);
* explicit, non-retried failures for auth or access (401/403/404).

Everything the policy needs but can't read is recorded as **unverified**, and the policy
fails closed on it. It has no write methods.
"""

from __future__ import annotations

import os
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from release_orchestrator.errors import IntegrationError
from release_orchestrator.integrations.github import Transport, urllib_transport

PERMISSION_RANK = {"none": 0, "read": 1, "triage": 2, "write": 3, "maintain": 4, "admin": 5}


class GitHubAccessError(IntegrationError):
    """401/403/404: not retried. The message never contains the token."""

    code = "GITHUB_ACCESS_DENIED"


@dataclass(frozen=True)
class PullStatus:
    repository: str
    number: int
    state: str  # open | closed
    draft: bool
    merged: bool
    head_ref: str
    head_sha: str
    base_ref: str
    author: str
    url: str


@dataclass(frozen=True)
class Review:
    user: str
    state: str  # APPROVED | CHANGES_REQUESTED | COMMENTED | DISMISSED | PENDING
    commit_id: str
    submitted_at: str
    association: str = ""  # author_association, recorded for audit


@dataclass(frozen=True)
class CheckRun:
    name: str
    status: str  # queued | in_progress | completed
    conclusion: str | None
    started_at: str = ""


@dataclass
class PullEvidence:
    pull: PullStatus
    reviews: list[Review]
    checks: list[CheckRun]
    fetched_at: str
    statuses: dict[str, str] = field(default_factory=dict)  # legacy commit status contexts
    permissions: dict[str, str] = field(default_factory=dict)  # reviewer -> level|unverified
    protection_checks: list[str] | None = None  # required checks from branch protection
    protection_note: str = "not requested"
    source: str = "github-rest"

    def to_dict(self) -> dict[str, Any]:
        return {
            "pull": asdict(self.pull),
            "reviews": [asdict(r) for r in self.reviews],
            "checks": [asdict(c) for c in self.checks],
            "statuses": self.statuses,
            "permissions": self.permissions,
            "branch_protection": {
                "required_checks": self.protection_checks,
                "note": self.protection_note,
            },
            "fetched_at": self.fetched_at,
            "source": self.source,
        }


class GitHubReader:
    def __init__(
        self,
        *,
        api_url: str = "https://api.github.com",
        token: str | None = None,
        transport: Transport = urllib_transport,
        attempts: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self._token = token if token is not None else os.environ.get("GITHUB_TOKEN")
        self._transport = transport
        self._attempts = attempts
        self._sleep = sleep

    @property
    def authenticated(self) -> bool:
        return bool(self._token)

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "library-release-orchestrator",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _get(self, path: str) -> Any:
        last = ""
        for attempt in range(1, self._attempts + 1):
            try:
                status, data = self._transport(
                    "GET", f"{self.api_url}{path}", self._headers(), None
                )
            except IntegrationError as exc:  # network failure: transient
                status, data, last = 0, None, exc.message
            message = data.get("message", "") if isinstance(data, dict) else ""
            if status == 200:
                return data
            if status in (401, 403, 404) and "rate limit" not in message.lower():
                hint = "" if self._token else " (no GITHUB_TOKEN set; private data needs one)"
                raise GitHubAccessError(
                    f"GitHub GET {path} returned {status}: {message}{hint}",
                    details={"status": status, "path": path, "authenticated": self.authenticated},
                )
            last = last or f"{status}: {message}"
            if attempt < self._attempts:
                self._sleep(2.0 ** (attempt - 1))
        raise IntegrationError(
            f"GitHub GET {path} failed after {self._attempts} attempts: {last}",
            details={"path": path, "retryable": True},
        )

    def pull(self, repository: str, number: int) -> PullStatus:
        d = self._get(f"/repos/{_repo(repository)}/pulls/{int(number)}")
        return PullStatus(
            repository=repository,
            number=int(d["number"]),
            state=str(d["state"]),
            draft=bool(d.get("draft", False)),
            merged=bool(d.get("merged", False)),
            head_ref=str(d["head"]["ref"]),
            head_sha=str(d["head"]["sha"]),
            base_ref=str(d["base"]["ref"]),
            author=str((d.get("user") or {}).get("login", "")),
            url=str(d["html_url"]),
        )

    def reviews(self, repository: str, number: int) -> list[Review]:
        data = self._get(f"/repos/{_repo(repository)}/pulls/{int(number)}/reviews?per_page=100")
        return [
            Review(
                user=str((r.get("user") or {}).get("login", "")),
                state=str(r.get("state", "")),
                commit_id=str(r.get("commit_id") or ""),
                submitted_at=str(r.get("submitted_at") or ""),
                association=str(r.get("author_association") or ""),
            )
            for r in data
        ]

    def check_runs(self, repository: str, sha: str) -> list[CheckRun]:
        _sha(sha)
        data = self._get(f"/repos/{_repo(repository)}/commits/{sha}/check-runs?per_page=100")
        return [
            CheckRun(
                str(c["name"]),
                str(c["status"]),
                c.get("conclusion"),
                str(c.get("started_at") or ""),
            )
            for c in data.get("check_runs", [])
        ]

    def commit_statuses(self, repository: str, sha: str) -> dict[str, str]:
        """Legacy commit statuses (``context -> state``), latest per context."""
        _sha(sha)
        data = self._get(f"/repos/{_repo(repository)}/commits/{sha}/status")
        return {str(s["context"]): str(s["state"]) for s in data.get("statuses", [])}

    def permission(self, repository: str, user: str) -> str:
        quoted = urllib.parse.quote(user, safe="")
        data = self._get(f"/repos/{_repo(repository)}/collaborators/{quoted}/permission")
        return str(data.get("permission", "none"))

    def protection_required_checks(self, repository: str, branch: str) -> list[str]:
        quoted = urllib.parse.quote(branch, safe="")
        data = self._get(f"/repos/{_repo(repository)}/branches/{quoted}/protection")
        checks = (data.get("required_status_checks") or {}).get("contexts") or []
        return [str(c) for c in checks]

    def evidence(self, repository: str, number: int) -> PullEvidence:
        pull = self.pull(repository, number)
        reviews = self.reviews(repository, number)
        ev = PullEvidence(
            pull=pull,
            reviews=reviews,
            checks=self.check_runs(repository, pull.head_sha),
            statuses=self.commit_statuses(repository, pull.head_sha),
            fetched_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        # Permission only matters for reviewers who approved; never assume it.
        for user in sorted({r.user for r in reviews if r.state == "APPROVED"} - {pull.author}):
            try:
                ev.permissions[user] = self.permission(repository, user)
            except GitHubAccessError as exc:
                ev.permissions[user] = f"unverified ({exc.details.get('status')})"
        try:
            ev.protection_checks = self.protection_required_checks(repository, pull.base_ref)
            ev.protection_note = "read"
        except GitHubAccessError as exc:
            # Unreadable is NOT the same as unprotected: record it, never infer "none".
            ev.protection_note = f"unverified ({exc.details.get('status')})"
        return ev


def _repo(repository: str) -> str:
    owner, _, name = repository.partition("/")
    if not owner or not name or "/" in name:
        raise ValueError(f"repository must be 'owner/name', got {repository!r}")
    return f"{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(name, safe='')}"


def _sha(sha: str) -> None:
    if len(sha) != 40 or not all(c in "0123456789abcdef" for c in sha):
        raise ValueError("sha must be a full 40-character hex commit id")


# --------------------------------------------------------------------------- approval policy
@dataclass(frozen=True)
class ApprovalPolicy:
    required_approvals: int = 1
    required_checks: tuple[str, ...] = ()
    require_ready_for_review: bool = True  # a draft PR is not an approved release
    min_reviewer_permission: str = "write"
    require_branch_protection_evidence: bool = False


@dataclass
class ApprovalDecision:
    approved: bool
    head_sha: str
    approvers: list[str] = field(default_factory=list)
    stale_approvals: list[str] = field(default_factory=list)
    unauthorized_approvals: list[str] = field(default_factory=list)
    changes_requested_by: list[str] = field(default_factory=list)
    checks: dict[str, str] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_approval(
    evidence: PullEvidence, policy: ApprovalPolicy, *, expected_head: str
) -> ApprovalDecision:
    """Deterministic and fail-closed: the same evidence and policy give the same decision.

    * The PR must be open, not merged, not a draft (by policy), and its head must be the
      verified release commit.
    * Only each reviewer's latest decisive review (APPROVED / CHANGES_REQUESTED /
      DISMISSED) counts, so a dismissal revokes an earlier approval. An approval counts
      only if it is on the current head (else **stale**) and the reviewer's repository
      permission is verified at least ``min_reviewer_permission`` (else **unauthorized**,
      including when the permission can't be read). The author never counts.
    * Required checks = the policy's checks plus branch-protection checks when readable.
      Each must have succeeded on the head, as the latest check run or a commit status.
      Unreadable branch protection fails when the policy requires that evidence.
    """
    pull = evidence.pull
    decision = ApprovalDecision(approved=False, head_sha=pull.head_sha)
    if pull.merged or pull.state != "open":
        decision.reasons.append(f"PR #{pull.number} is {'merged' if pull.merged else pull.state}")
    if pull.draft and policy.require_ready_for_review:
        decision.reasons.append(f"PR #{pull.number} is still a draft")
    if pull.head_sha != expected_head:
        decision.reasons.append(
            f"PR head {pull.head_sha[:10]} is not the verified release commit {expected_head[:10]}"
        )

    minimum = PERMISSION_RANK.get(policy.min_reviewer_permission, PERMISSION_RANK["admin"])
    latest: dict[str, Review] = {}
    for review in sorted(evidence.reviews, key=lambda r: r.submitted_at):
        if review.state in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest[review.user] = review
    for user, review in sorted(latest.items()):
        if user == pull.author:
            continue
        if review.state == "CHANGES_REQUESTED":
            decision.changes_requested_by.append(user)
        elif review.state == "APPROVED":
            level = evidence.permissions.get(user, "unverified")
            if review.commit_id != pull.head_sha:
                decision.stale_approvals.append(f"{user}@{review.commit_id[:10]}")
            elif PERMISSION_RANK.get(level, -1) < minimum:
                decision.unauthorized_approvals.append(f"{user} ({level})")
            else:
                decision.approvers.append(user)
    if decision.changes_requested_by:
        decision.reasons.append(f"changes requested by {decision.changes_requested_by}")
    if len(decision.approvers) < policy.required_approvals:
        detail = ""
        if decision.stale_approvals:
            detail += f"; stale: {decision.stale_approvals}"
        if decision.unauthorized_approvals:
            detail += (
                f"; not counted (permission below {policy.min_reviewer_permission} or "
                f"unverifiable): {decision.unauthorized_approvals}"
            )
        decision.reasons.append(
            f"{len(decision.approvers)} valid approval(s), {policy.required_approvals} required"
            + detail
        )

    required = list(policy.required_checks)
    if evidence.protection_checks is not None:
        required += [c for c in evidence.protection_checks if c not in required]
    elif policy.require_branch_protection_evidence:
        decision.reasons.append(
            f"branch protection could not be read ({evidence.protection_note}); policy requires it"
        )
    runs: dict[str, CheckRun] = {}
    for check in sorted(evidence.checks, key=lambda c: c.started_at):
        runs[check.name] = check  # latest run per name
    for name in required:
        run = runs.get(name)
        if run is not None:
            decision.checks[name] = str(run.conclusion) if run.status == "completed" else run.status
        elif name in evidence.statuses:
            decision.checks[name] = evidence.statuses[name]  # success|failure|error|pending
        else:
            decision.checks[name] = "missing"
    failing = {n: s for n, s in decision.checks.items() if s != "success"}
    if failing:
        decision.reasons.append(f"required checks not successful: {failing}")

    decision.approved = not decision.reasons
    return decision
