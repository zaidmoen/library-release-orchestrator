"""Publication requires a verified GitHub approval (ADR-0011); --approve alone is not enough."""

from __future__ import annotations

from pathlib import Path

import pytest

from release_orchestrator.errors import BlockedError
from release_orchestrator.integrations.github_status import GitHubReader
from release_orchestrator.lab import build_lab
from release_orchestrator.state import RunStatus
from tests.lab.test_lab_scenarios import Lab

pytestmark = pytest.mark.integration

POLICY = """approval_policy:
  required_approvals: 1
  required_checks: [ci]
"""


class FakeGitHub:
    """Serves PR #5 for the lab library; head/reviews/checks are set per test."""

    def __init__(self, head: str) -> None:
        self.head, self.draft, self.reviews, self.checks = head, False, [], []
        self.permission: tuple[int, dict[str, str]] = (200, {"permission": "write"})
        self.calls: list[str] = []

    def __call__(self, method, url, headers, body):  # type: ignore[no-untyped-def]
        assert method == "GET"  # the reader never writes
        self.calls.append(url)
        if url.endswith("/pulls/5"):
            return 200, {
                "number": 5,
                "state": "open",
                "draft": self.draft,
                "merged": False,
                "head": {"ref": "release/1.1.0", "sha": self.head},
                "base": {"ref": "release-base/v1.0.0"},
                "user": {"login": "releaser"},
                "html_url": "https://github.com/zaidmoen/harri-release-lab-library/pull/5",
            }
        if "/pulls/5/reviews" in url:
            return 200, self.reviews
        if "/check-runs" in url:
            return 200, {"check_runs": self.checks}
        if url.endswith("/status"):
            return 200, {"statuses": []}
        if url.endswith("/permission"):
            return self.permission
        return 404, {"message": "Not Found"}


def _lab(tmp_path: Path, extra: str) -> Lab:
    root = tmp_path / "lab"
    config = build_lab(root, provider="external")
    text = config.read_text().replace(
        "pull_requests:\n  provider: external\n",
        "pull_requests:\n  provider: external\n  github_repositories:\n"
        "    zaidlibrary: zaidmoen/harri-release-lab-library\n",
    )
    config.write_text(text + extra)
    return Lab(root, config)


def _validated(lab: Lab):  # type: ignore[no-untyped-def]
    plan = lab.plan("FIN-204")
    lab.orch.prepare(plan, approve=True, dry_run=False)
    lab.orch.validate(plan)
    lab.orch.open_pr(plan, approve=True)
    head = lab.git("harri-release-lab-library", "rev-parse", plan.branch)
    fake = FakeGitHub(head)
    lab.orch.s.github = GitHubReader(transport=fake, sleep=lambda _s: None)
    return plan, head, fake


def _record(lab: Lab, plan) -> None:  # type: ignore[no-untyped-def]
    lab.orch.record_pr(
        plan,
        repository="zaidlibrary",
        head=plan.branch,
        number=5,
        url="https://github.com/zaidmoen/harri-release-lab-library/pull/5",
        state="open",
    )


def _approved(head: str):  # type: ignore[no-untyped-def]
    return [
        {
            "user": {"login": "reviewer"},
            "state": "APPROVED",
            "commit_id": head,
            "submitted_at": "2026-11-10T10:00:00Z",
        }
    ]


GREEN = [
    {
        "name": "ci",
        "status": "completed",
        "conclusion": "success",
        "started_at": "2026-11-10T09:00:00Z",
    }
]


def test_publish_without_recorded_pr_is_refused(tmp_path):
    lab = _lab(tmp_path, POLICY)
    plan, _, _ = _validated(lab)
    with pytest.raises(BlockedError) as exc:
        lab.orch.publish(plan, approve=True)
    assert exc.value.reason == "PR_NOT_RECORDED"
    assert lab.orch.s.registry.list_versions("zaidlibrary") == []


@pytest.mark.parametrize(
    "case",
    [
        "draft",
        "no_review",
        "stale",
        "failed_check",
        "changes",
        "read_only",
        "perm_hidden",
        "dismissed",
    ],
)
def test_unverified_approval_blocks_publication(tmp_path, case):
    lab = _lab(tmp_path, POLICY)
    plan, head, fake = _validated(lab)
    _record(lab, plan)
    fake.reviews, fake.checks = _approved(head), GREEN
    if case == "draft":
        fake.draft = True
    elif case == "no_review":
        fake.reviews = []
    elif case == "stale":
        fake.reviews = _approved("0" * 40)
    elif case == "failed_check":
        fake.checks = [{**GREEN[0], "conclusion": "failure"}]
    elif case == "read_only":
        fake.permission = (200, {"permission": "read"})
    elif case == "perm_hidden":
        fake.permission = (403, {"message": "Must have push access"})
    elif case == "dismissed":
        fake.reviews = [
            *_approved(head),
            {
                "user": {"login": "reviewer"},
                "state": "DISMISSED",
                "commit_id": head,
                "submitted_at": "2026-11-10T12:00:00Z",
            },
        ]
    elif case == "changes":
        fake.reviews = [
            *_approved(head),
            {
                "user": {"login": "lead"},
                "state": "CHANGES_REQUESTED",
                "commit_id": head,
                "submitted_at": "2026-11-10T11:00:00Z",
            },
        ]
    with pytest.raises(BlockedError) as exc:
        lab.orch.publish(plan, approve=True)
    assert exc.value.reason == "APPROVAL_NOT_VERIFIED"
    assert lab.orch.s.registry.list_versions("zaidlibrary") == []
    assert lab.git("harri-release-lab-library", "tag", "--list", "v1.1.0") == ""
    assert lab.orch.status(plan).state is RunStatus.BLOCKED


def test_verified_approval_allows_publication_and_is_audited(tmp_path):
    lab = _lab(tmp_path, POLICY)
    plan, head, fake = _validated(lab)
    _record(lab, plan)
    fake.reviews, fake.checks = _approved(head), GREEN
    assert lab.orch.publish(plan, approve=True)["status"] == "published"
    evidence = lab.orch.status(plan).steps["pr-status"]["result"]
    assert evidence["decision"]["approved"] is True
    assert evidence["decision"]["approvers"] == ["reviewer"]
    assert evidence["evidence"]["pull"]["head_sha"] == head


def test_operator_approve_flag_is_still_required(tmp_path):
    from release_orchestrator.errors import ApprovalRequiredError

    lab = _lab(tmp_path, POLICY)
    plan, head, fake = _validated(lab)
    _record(lab, plan)
    fake.reviews, fake.checks = _approved(head), GREEN
    with pytest.raises(ApprovalRequiredError):
        lab.orch.publish(plan, approve=False)


def test_production_registry_without_policy_rejects_publish(tmp_path):
    root = tmp_path / "lab"
    config = build_lab(root)
    config.write_text(
        config.read_text().replace(
            "registry:\n  provider: local\n", "registry:\n  provider: local\n  production: true\n"
        )
    )
    lab = Lab(root, config)
    plan = lab.plan("FIN-204")
    lab.orch.prepare(plan, approve=True, dry_run=False)
    lab.orch.validate(plan)
    with pytest.raises(BlockedError) as exc:
        lab.orch.publish(plan, approve=True)
    assert exc.value.reason == "PRODUCTION_PUBLISH_UNAUTHORIZED"
    assert lab.orch.s.registry.list_versions("zaidlibrary") == []


def test_production_registry_is_refused_even_with_verified_github_approval(tmp_path):
    lab = _lab(tmp_path, POLICY)
    config = lab.config_path
    config.write_text(
        config.read_text().replace(
            "registry:\n  provider: local\n", "registry:\n  provider: local\n  production: true\n"
        )
    )
    lab.reload()
    plan, head, fake = _validated(lab)
    _record(lab, plan)
    fake.reviews, fake.checks = _approved(head), GREEN
    with pytest.raises(BlockedError) as exc:
        lab.orch.publish(plan, approve=True)
    assert exc.value.reason == "PRODUCTION_PUBLISH_UNAUTHORIZED"
    assert lab.orch.s.registry.list_versions("zaidlibrary") == []
