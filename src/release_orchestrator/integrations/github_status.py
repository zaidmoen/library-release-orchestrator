"""Authoritative pull request state from the GitHub REST API (read-only, ADR-0011).

Git refs (ADR-0010) prove a PR exists and which commit it points at. Only the API knows
whether it is open, draft or merged, who reviewed which commit, and how the checks
concluded. This reader fetches exactly that, with:

* an optional token from ``GITHUB_TOKEN`` (public repositories work anonymously; private
  ones need a token with read access to pull requests and checks only);
* bounded retries for transient failures (network, 5xx, secondary rate limit);
* explicit, non-retried failures for auth (401), no access or not found (403/404).

It has no write methods.
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
    source: str = "github-rest"

    def to_dict(self) -> dict[str, Any]:
        return {
            "pull": asdict(self.pull),
            "reviews": [asdict(r) for r in self.reviews],
            "checks": [asdict(c) for c in self.checks],
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
                hint = "" if self._token else " (no GITHUB_TOKEN set; private repos need one)"
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
            )
            for r in data
        ]

    def check_runs(self, repository: str, sha: str) -> list[CheckRun]:
        if not all(c in "0123456789abcdef" for c in sha) or len(sha) != 40:
            raise ValueError("sha must be a full 40-character hex commit id")
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

    def evidence(self, repository: str, number: int) -> PullEvidence:
        pull = self.pull(repository, number)
        return PullEvidence(
            pull=pull,
            reviews=self.reviews(repository, number),
            checks=self.check_runs(repository, pull.head_sha),
            fetched_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )


def _repo(repository: str) -> str:
    owner, _, name = repository.partition("/")
    if not owner or not name or "/" in name:
        raise ValueError(f"repository must be 'owner/name', got {repository!r}")
    return f"{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(name, safe='')}"


# --------------------------------------------------------------------------- approval policy
@dataclass(frozen=True)
class ApprovalPolicy:
    required_approvals: int = 1
    required_checks: tuple[str, ...] = ()
    require_ready_for_review: bool = True  # a draft PR is not an approved release


@dataclass
class ApprovalDecision:
    approved: bool
    head_sha: str
    approvers: list[str] = field(default_factory=list)
    stale_approvals: list[str] = field(default_factory=list)
    changes_requested_by: list[str] = field(default_factory=list)
    checks: dict[str, str] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_approval(
    evidence: PullEvidence, policy: ApprovalPolicy, *, expected_head: str
) -> ApprovalDecision:
    """Deterministic: the same evidence and policy always give the same decision.

    * Only each reviewer's **latest** review counts. ``APPROVED`` counts only if it was
      given on the PR's current head commit, which must equal ``expected_head`` (the
      verified release commit). Approvals of older commits are reported as stale.
    * Any reviewer whose latest review is ``CHANGES_REQUESTED`` blocks.
    * The PR author never counts as an approver.
    * Every required check must have completed with ``success`` on the head commit.
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
            if review.commit_id == pull.head_sha:
                decision.approvers.append(user)
            else:
                decision.stale_approvals.append(f"{user}@{review.commit_id[:10]}")
    if decision.changes_requested_by:
        decision.reasons.append(f"changes requested by {decision.changes_requested_by}")
    if len(decision.approvers) < policy.required_approvals:
        decision.reasons.append(
            f"{len(decision.approvers)} current approval(s), {policy.required_approvals} required"
            + (f"; stale: {decision.stale_approvals}" if decision.stale_approvals else "")
        )

    # A check can run several times on one commit (re-runs, push + pull_request events):
    # only the most recently started run of each name counts.
    runs: dict[str, CheckRun] = {}
    for check in sorted(evidence.checks, key=lambda c: c.started_at):
        runs[check.name] = check
    for name in policy.required_checks:
        run = runs.get(name)
        if run is None:
            decision.checks[name] = "missing"
        elif run.status != "completed":
            decision.checks[name] = run.status
        else:
            decision.checks[name] = str(run.conclusion)
    failing = {n: s for n, s in decision.checks.items() if s != "success"}
    if failing:
        decision.reasons.append(f"required checks not successful: {failing}")

    decision.approved = not decision.reasons
    return decision
