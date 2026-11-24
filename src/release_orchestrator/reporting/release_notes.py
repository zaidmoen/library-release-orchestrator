"""GitHub Release Notes from the **actual release content** (ADR-0017).

Selective releases are the point of this tool, so the notes are built from
``plan.included`` only: the changes cherry-picked into this release. Excluded changes,
other tickets on ``main``, and the contents of older candidates never appear as shipped.

Sources, each used only for what it proves:

* commits of the included changes: category (Conventional Commits type), breaking changes
  (``!:`` or a ``BREAKING CHANGE:`` footer), references;
* the dependency analysis: public API removals (breaking, with the symbol);
* the issue tracker (MCP snapshot or fixture): summary and status; inaccessible or
  unknown tickets are marked **unresolved**, never filled in;
* PR evidence (MCP / REST / snapshot): merged PRs whose title or branch carries the key;
* the impact matrix: consumers that need an update, with the exact pin to change;
* the run state: validation, publication and tag.

Publishing creates or updates a **draft** GitHub Release, and only after: explicit
approval, the artifact is published, the tag exists locally at the release head, and the
same tag is on the remote at the same commit. A published (non-draft) GitHub Release is
never modified. Nothing is invented: no links without a source.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

from release_orchestrator.errors import (
    ApprovalRequiredError,
    BlockedError,
    ConfigError,
    GitError,
    IntegrationError,
)
from release_orchestrator.gitutil.repo import Git
from release_orchestrator.integrations.github import Transport, urllib_transport
from release_orchestrator.integrations.github_status import PullSummary
from release_orchestrator.integrations.jira import IssueTracker
from release_orchestrator.integrations.linkage import extract_keys, own_keys
from release_orchestrator.planning.plan import ReleasePlan

# "feat(x): y", also after a ticket prefix: "FIN-204: feat(x): y" / "[FIN-204] feat: y"
_CC = re.compile(
    r"^(?:\[?[A-Z][A-Z0-9]+-\d+\]?:?\s+)?(?P<type>[a-z]+)(?:\([^)]*\))?(?P<bang>!)?:\s+"
    r"(?P<text>.*)$"
)
_SECTIONS = {"feat": "Features", "fix": "Fixes"}


@dataclass
class NoteEntry:
    key: str
    title: str
    section: str
    status: str | None
    commits: list[str]
    pull_requests: list[dict[str, Any]]
    branches: list[str]
    unresolved: bool


def _clean_title(subject: str) -> str:
    """The commit subject without the ticket prefix and Conventional Commit type."""
    match = _CC.match(subject)
    if match:
        return match.group("text").strip()
    return re.sub(r"^\[?[A-Z][A-Z0-9]+-\d+\]?:?\s+", "", subject).strip()


def _section(subject: str) -> str:
    match = _CC.match(subject)
    return _SECTIONS.get(match.group("type"), "Improvements") if match else "Improvements"


def build_notes(
    plan: ReleasePlan,
    git: Git,
    *,
    tag_prefix: str,
    tracker: IssueTracker,
    pulls: list[PullSummary],
    impact: dict[str, Any] | None,
    run_steps: dict[str, Any],
    key_pattern: str,
) -> dict[str, Any]:
    entries: list[NoteEntry] = []
    breaking: list[str] = []
    for change in plan.included:
        issue = tracker.get(change.key)
        subjects = [c.subject for c in change.commits]
        section = _section(subjects[0]) if subjects else "Improvements"
        for commit in change.commits:
            info = git.commit_info(commit.sha)
            match = _CC.match(info.subject)
            if (match and match.group("bang")) or "BREAKING CHANGE:" in info.body:
                footer = re.search(r"BREAKING CHANGE:\s*(.+)", info.body)
                detail = footer.group(1).strip() if footer else info.subject
                breaking.append(f"{change.key}: {detail[:300]}")
        prs = []
        branches = []
        for pr in pulls:
            if pr.merged and change.key in own_keys(extract_keys(pr, key_pattern)):
                prs.append({"number": pr.number, "url": pr.url, "branch": pr.head_ref})
                branches.append(pr.head_ref)
        entries.append(
            NoteEntry(
                key=change.key,
                title=(
                    issue.summary
                    if issue and issue.summary
                    else _clean_title(subjects[0] if subjects else change.title)
                )[:200],
                section=section,
                status=issue.status if issue else None,
                commits=[c.sha for c in change.commits],
                pull_requests=prs,
                branches=sorted(set(branches)),
                unresolved=issue is None or not issue.accessible,
            )
        )
    for symbol in plan.analysis.get("public_api_removals", []):
        breaking.append(f"public symbol `{symbol}` was removed")

    publish = run_steps.get("publish", {}).get("result", {})
    validate = run_steps.get("validate", {}).get("result", {})
    tag = f"{tag_prefix}{plan.proposed_version}"
    consumers = []
    for row in (impact or {}).get("rows", []):
        if row["decision"] in ("UPGRADE_RECOMMENDED", "BREAKING_IMPACT", "UNRESOLVED"):
            consumers.append(row)
    return {
        "version": plan.proposed_version,
        "tag": tag,
        "plan_id": plan.plan_id,
        "base": {"ref": plan.base_ref, "revision": plan.base_revision},
        "entries": [vars(e) for e in entries],
        "breaking_changes": breaking,
        "consumer_impact": consumers,
        "validation": validate.get("status", "not run"),
        "published": publish.get("status") in ("published", "already_published"),
        "excluded": [e.key for e in plan.excluded],  # for reviewers only; not in the notes
        "unresolved_tickets": [e.key for e in entries if e.unresolved],
    }


def render_notes(notes: dict[str, Any], *, library: str) -> str:
    entries = notes["entries"]
    count = len(entries)
    lines = [
        f"# Release {notes['tag']}",
        "",
        "## Summary",
        "",
        f"{library} {notes['version']} ships {count} change{'s' if count != 1 else ''} "
        f"on top of `{notes['base']['ref']}`"
        + (", including **breaking changes**." if notes["breaking_changes"] else "."),
    ]
    for section in ("Features", "Fixes", "Improvements"):
        items = [e for e in entries if e["section"] == section]
        if items:
            lines += ["", f"## {section}", ""]
            for e in items:
                suffix = " _(ticket details unresolved)_" if e["unresolved"] else ""
                lines.append(f"- **{e['key']}**: {e['title']}{suffix}")
    lines += ["", "## Breaking Changes", ""]
    lines += [f"- {b}" for b in notes["breaking_changes"]] or ["- None identified."]
    lines += ["", "## Consumer Impact", ""]
    if notes["consumer_impact"]:
        for row in notes["consumer_impact"]:
            lines.append(
                f"- **{row['service']}** ({row['decision']}): {row['required_action']}; "
                f"current {row['current_version'] or 'unknown'} → {row['proposed_version']}"
            )
    else:
        lines.append("- No consumer requires an update according to the available evidence.")
    lines += [
        "",
        "## Validation",
        "",
        f"- Library validation: {notes['validation']}",
        f"- Artifact published: {'yes' if notes['published'] else 'no'}",
        "",
        "## References",
        "",
    ]
    for e in entries:
        refs = [f"commits {', '.join(s[:10] for s in e['commits'])}"]
        refs += [
            f"[#{p['number']}]({p['url']})" if p["url"] else f"#{p['number']}"
            for p in e["pull_requests"]
        ]
        if e["branches"]:
            refs.append(f"branches {', '.join(e['branches'])}")
        lines.append(f"- {e['key']}: {'; '.join(refs)}")
    return "\n".join(lines) + "\n"


class GitHubReleaseWriter:
    """Creates or updates a **draft** GitHub Release over REST (the GitHub MCP tools
    available here have no release-write operation). Token from ``GITHUB_TOKEN``; it's
    never printed."""

    def __init__(
        self,
        *,
        api_url: str = "https://api.github.com",
        token: str | None = None,
        transport: Transport = urllib_transport,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self._token = token if token is not None else os.environ.get("GITHUB_TOKEN", "")
        self._transport = transport

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
        if not self._token:
            raise ConfigError("GITHUB_TOKEN is required to write a GitHub Release")
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "library-release-orchestrator",
        }
        return self._transport(method, f"{self.api_url}{path}", headers, body)

    def upsert_draft(
        self, repository: str, *, tag: str, name: str, body: str, prerelease: bool
    ) -> dict[str, Any]:
        status, data = self._call("GET", f"/repos/{repository}/releases/tags/{tag}")
        if status == 200:
            if not data.get("draft", False):
                raise BlockedError(
                    f"GitHub Release {tag} is already published; it is never modified",
                    reason="RELEASE_ALREADY_PUBLISHED",
                    details={"url": data.get("html_url")},
                )
            status, data = self._call(
                "PATCH", f"/repos/{repository}/releases/{int(data['id'])}", {"body": body}
            )
            action = "updated draft"
        elif status == 404:
            status, data = self._call(
                "POST",
                f"/repos/{repository}/releases",
                {
                    "tag_name": tag,
                    "name": name,
                    "body": body,
                    "draft": True,
                    "prerelease": prerelease,
                },
            )
            action = "created draft"
        else:
            raise IntegrationError(f"GitHub Release lookup failed with HTTP {status}")
        if status not in (200, 201):
            raise IntegrationError(f"GitHub Release {action} failed with HTTP {status}")
        return {"action": action, "url": data.get("html_url"), "draft": True}


def publish_notes(
    plan: ReleasePlan,
    git: Git,
    *,
    notes_markdown: str,
    repository: str,
    tag: str,
    run_steps: dict[str, Any],
    remote: str,
    approve: bool,
    writer: GitHubReleaseWriter,
) -> dict[str, Any]:
    """Gate checks, then the draft GitHub Release. Every precondition is verified, not
    assumed; a failure says exactly which one."""
    if not approve:
        raise ApprovalRequiredError(
            "Writing a GitHub Release needs an explicit human yes (--approve)",
            details={"tag": tag, "repository": repository},
        )
    publish = run_steps.get("publish", {}).get("result", {})
    if publish.get("status") not in ("published", "already_published"):
        raise BlockedError(
            "The artifact isn't published yet; notes can't describe an unpublished release",
            reason="RELEASE_NOT_PUBLISHED",
        )
    release_head = publish.get("artifact", {}).get("metadata", {}).get("release_commit")
    local = git.try_rev_parse(f"refs/tags/{tag}")
    if local is None or (release_head and local != release_head):
        raise BlockedError(
            f"Tag {tag} is missing locally or doesn't point at the release commit",
            reason="TAG_MISMATCH",
            details={"tag_commit": local, "release_commit": release_head},
        )
    try:
        remote_sha = git.remote_ref(remote, f"refs/tags/{tag}")
        peeled = git.remote_ref(remote, f"refs/tags/{tag}^{{}}")
    except GitError as exc:
        raise BlockedError(
            f"Can't verify tag {tag} on '{remote}': the remote is unreachable or missing",
            reason="TAG_NOT_PUSHED",
            details={"remote": remote, "error": exc.message[:300]},
        ) from exc
    if (peeled or remote_sha) != local:
        raise BlockedError(
            f"Tag {tag} isn't on '{remote}' at the release commit; push it first (never forced)",
            reason="TAG_NOT_PUSHED",
            details={"local": local, "remote": peeled or remote_sha},
        )
    prerelease = "-" in plan.proposed_version
    return writer.upsert_draft(
        repository, tag=tag, name=tag, body=notes_markdown, prerelease=prerelease
    )
