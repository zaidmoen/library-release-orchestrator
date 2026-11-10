"""Pull request discovery and reconciliation through Git itself (ADR-0010).

GitHub advertises every pull request as ``refs/pull/<n>/head`` to anyone who can read the
repository, so PR *existence* and *head commit* can be verified with the same Git
credentials used for pushing, with no API token and no MCP access needed. This lets the
engine:

* find a PR that a human (or Claude through MCP) created, and record it automatically;
* prevent duplicate creation: a matching PR is reported before anyone opens another;
* detect drift: a recorded PR whose ref vanished, or whose head no longer matches.

Limits (documented, not hidden): Git refs don't say whether a PR is open, closed, draft
or merged. Closed PRs keep their ``refs/pull/<n>/head``. Status beyond existence needs
the GitHub API (MCP or REST).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

from release_orchestrator.config import normalize_remote
from release_orchestrator.gitutil.repo import Git
from release_orchestrator.integrations.pull_requests import ExternalPullRequestProvider

_PULL_REF = re.compile(r"^refs/pull/(\d+)/head$")

# Reconciliation outcomes
VERIFIED = "recorded_verified"
HEAD_MOVED = "recorded_head_moved"
MISSING = "recorded_missing"
DISCOVERED = "discovered"
AMBIGUOUS = "ambiguous"
NOT_CREATED = "not_created"
BRANCH_NOT_PUSHED = "branch_not_pushed"
NO_REQUEST = "no_request"


@dataclass(frozen=True)
class ReconcileResult:
    repository: str
    head: str
    status: str
    number: int | None = None
    url: str | None = None
    head_sha: str | None = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        """True when the recorded state matches GitHub, or nothing needs to exist yet."""
        return self.status in (VERIFIED, DISCOVERED, NOT_CREATED)

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "ok": self.ok}


def remote_pull_requests(git: Git, remote: str) -> dict[int, str]:
    """``{pr_number: head_sha}`` for every PR the remote advertises."""
    prs: dict[int, str] = {}
    for sha, ref in git.ls_remote(remote, "refs/pull/*/head"):
        match = _PULL_REF.match(ref)
        if match:
            prs[int(match.group(1))] = sha
    return prs


def pull_request_url(remote_url: str, number: int) -> str:
    normalized = normalize_remote(remote_url)
    if normalized.startswith("github.com/"):
        return f"https://{normalized}/pull/{number}"
    return f"{normalized}#pull/{number}"  # local bare remotes used in tests


def reconcile(
    provider: ExternalPullRequestProvider,
    *,
    repository: str,
    head: str,
    git: Git,
    remote: str,
) -> ReconcileResult:
    request = provider.get(repository, head)
    if request is None:
        return ReconcileResult(
            repository, head, NO_REQUEST, detail="no PR request was prepared for this branch"
        )
    head_sha = git.remote_ref(remote, f"refs/heads/{head}")
    if head_sha is None:
        return ReconcileResult(
            repository, head, BRANCH_NOT_PUSHED, detail="push the branch before a PR can exist"
        )
    prs = remote_pull_requests(git, remote)
    number = request.get("number")

    if number is not None:
        url = request.get("url")
        if number not in prs:
            return ReconcileResult(
                repository,
                head,
                MISSING,
                number,
                url,
                head_sha,
                f"recorded PR #{number} is not advertised by the remote",
            )
        if prs[number] != head_sha:
            return ReconcileResult(
                repository,
                head,
                HEAD_MOVED,
                number,
                url,
                head_sha,
                f"PR #{number} points at {prs[number][:10]}, branch is at {head_sha[:10]}",
            )
        return ReconcileResult(repository, head, VERIFIED, number, url, head_sha)

    matches = sorted(n for n, sha in prs.items() if sha == head_sha)
    if not matches:
        return ReconcileResult(
            repository,
            head,
            NOT_CREATED,
            head_sha=head_sha,
            detail="no PR on the remote has this head commit yet",
        )
    if len(matches) > 1:
        return ReconcileResult(
            repository,
            head,
            AMBIGUOUS,
            head_sha=head_sha,
            detail=f"PRs {matches} all point at {head_sha[:10]}; record the right one "
            "with record-pr",
        )
    url = pull_request_url(git.remote_url(remote), matches[0])
    provider.record_discovered(repository, head, number=matches[0], url=url)
    return ReconcileResult(
        repository,
        head,
        DISCOVERED,
        matches[0],
        url,
        head_sha,
        "found on the remote and recorded; open/draft state not visible through Git",
    )
