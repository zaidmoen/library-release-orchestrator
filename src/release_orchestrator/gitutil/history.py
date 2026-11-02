"""Map Git history onto logical business changes.

A commit belongs to a change when the change key appears in its subject or in a
``Refs:`` trailer. Commits naming several keys are *ambiguous* and are never silently
assigned. Commits that already exist in the base (same ``patch-id``) are marked as
already released, which makes successive out-of-order releases safe.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from release_orchestrator.gitutil.repo import Git
from release_orchestrator.models import ChangeUnit, CommitInfo

_PICKED_RE = re.compile(r"\(cherry picked from commit ([0-9a-f]{40})\)")
_TRAILER_RE = re.compile(r"^(?:Refs?|Jira|Change-Key):\s*(.+)$", re.IGNORECASE | re.MULTILINE)


def extract_keys(commit: CommitInfo, key_pattern: str) -> list[str]:
    key_re = re.compile(rf"\b{key_pattern}\b")
    keys: dict[str, None] = {}
    for key in key_re.findall(commit.subject):
        keys.setdefault(key, None)
    for trailer in _TRAILER_RE.findall(commit.body):
        for key in key_re.findall(trailer):
            keys.setdefault(key, None)
    return list(keys)


@dataclass
class HistoryScan:
    base_ref: str
    base_sha: str
    source_ref: str
    source_sha: str
    changes: dict[str, ChangeUnit] = field(default_factory=dict)
    already_released: dict[str, list[str]] = field(default_factory=dict)
    unmapped: list[CommitInfo] = field(default_factory=list)
    ambiguous: dict[str, list[str]] = field(default_factory=dict)  # sha -> keys
    ambiguous_commits: list[CommitInfo] = field(default_factory=list)
    merges_skipped: list[str] = field(default_factory=list)
    patch_ids: dict[str, str] = field(default_factory=dict)  # sha -> patch-id

    def commit_order(self) -> list[str]:
        """Original topological order of every mapped commit (oldest first)."""
        order: list[tuple[int, str]] = []
        index = {sha: i for i, sha in enumerate(self.patch_ids)}
        for change in self.changes.values():
            order.extend((index[c.sha], c.sha) for c in change.commits)
        return [sha for _, sha in sorted(order)]

    def to_dict(self) -> dict[str, object]:
        return {
            "base_ref": self.base_ref,
            "base_revision": self.base_sha,
            "source_ref": self.source_ref,
            "source_revision": self.source_sha,
            "changes": [c.to_dict() for c in self.changes.values()],
            "already_released": self.already_released,
            "unmapped_commits": [c.to_dict() for c in self.unmapped],
            "ambiguous_commits": self.ambiguous,
            "merge_commits_skipped": self.merges_skipped,
        }


def scan_history(
    git: Git,
    *,
    base_ref: str,
    source_ref: str,
    key_pattern: str,
    titles: dict[str, str] | None = None,
) -> HistoryScan:
    base_sha = git.rev_parse(base_ref)
    source_sha = git.rev_parse(source_ref)
    scan = HistoryScan(base_ref, base_sha, source_ref, source_sha)

    # Patches already present in the base but not in the source (e.g. cherry-picks made
    # for an earlier out-of-order release). Matching patch-ids mean "already released".
    released_ids: set[str] = set()
    released_shas: set[str] = set()
    for sha in git.rev_list(f"{source_sha}..{base_sha}"):
        released_ids.add(git.patch_id(sha))
        released_shas.update(_PICKED_RE.findall(git.commit_info(sha).body))

    scan.merges_skipped = git.merge_commits(f"{base_sha}..{source_sha}")
    grouped: dict[str, list[CommitInfo]] = {}
    for sha in git.rev_list(f"{base_sha}..{source_sha}"):
        commit = git.commit_info(sha)
        patch_id = git.patch_id(sha)
        scan.patch_ids[sha] = patch_id
        keys = extract_keys(commit, key_pattern)
        if not keys:
            scan.unmapped.append(commit)
            continue
        if len(keys) > 1:
            scan.ambiguous[sha] = keys
            scan.ambiguous_commits.append(commit)
            continue
        key = keys[0]
        if sha in released_shas or (patch_id and patch_id in released_ids):
            scan.already_released.setdefault(key, []).append(sha)
            continue
        grouped.setdefault(key, []).append(commit)

    titles = titles or {}
    for key, commits in grouped.items():
        title = titles.get(key) or commits[0].subject
        scan.changes[key] = ChangeUnit(key=key, commits=tuple(commits), title=title)
    return scan
