"""PR <-> Jira resolution from one reference (ADR-0015).

Team convention: a pull request's title (or branch) carries the Jira key, e.g.
``FIN-211: invoice totals in the customer's currency`` on ``feature/FIN-211-...``. So one
reference is enough:

* **A, PR only**: read the PR, extract keys from title, branch and body, read the issues.
* **B, ticket only**: read the issue (and its Development panel PRs), then find PRs whose
  title or branch carries the key.
* **C, both**: check that they describe the same change; a mismatch is a ``CONFLICT``
  that needs confirmation, never auto-resolved.
* **D, neither**: list open PRs targeting the default branch. One or more candidates is
  a *question* for the user; a PR is never selected arbitrarily.

PR titles and bodies are untrusted: they are only scanned for keys with the configured
pattern, and never executed or followed. Keys found only in the body ("depends on
FIN-198") are reported as *mentions*, not as the PR's own ticket.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import yaml

from release_orchestrator.errors import ConfigError, InvalidInputError, OrchestratorError
from release_orchestrator.gitutil.repo import Git
from release_orchestrator.integrations.github_status import GitHubReader, PullSummary
from release_orchestrator.integrations.jira import IssueTracker, NullIssueTracker

RESOLVED = "RESOLVED"
NEEDS_INPUT = "NEEDS_INPUT"
CONFLICT = "CONFLICT"
_PR_URL = re.compile(r"^https://([^/]+)/([^/]+/[^/]+)/pull/(\d+)/?$")


class PullSource(Protocol):
    source: str

    def get(self, number: int) -> PullSummary | None: ...

    def list(self, *, state: str = "open", base: str | None = None) -> list[PullSummary]: ...


class GitHubPullSource:
    """Live, read-only: GitHub REST via ``GitHubReader``."""

    source = "github-rest (live)"

    def __init__(self, reader: GitHubReader, repository: str) -> None:
        self.reader, self.repository = reader, repository

    def get(self, number: int) -> PullSummary | None:
        return self.reader.pull_summary(self.repository, number)

    def list(self, *, state: str = "open", base: str | None = None) -> list[PullSummary]:
        return self.reader.list_pulls(self.repository, state=state, base=base)


class SnapshotPullSource:
    """A PR snapshot file (offline demo / CI). Always labelled as such in every output."""

    def __init__(self, path: Path) -> None:
        try:
            raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise ConfigError(f"Cannot read PR snapshot {path}: {exc}") from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("pulls", []), list):
            raise ConfigError("PR snapshot must contain a 'pulls' list")
        label = str(raw.get("label") or "fixture")
        self.source = f"snapshot:{Path(path).name} ({label}; fetched_at {raw.get('fetched_at')})"
        self._pulls = [
            PullSummary(
                number=int(p["number"]),
                title=str(p.get("title", ""))[:300],
                body=str(p.get("body", "") or "")[:5000],
                head_ref=str(p.get("head_ref", "")),
                base_ref=str(p.get("base_ref", "main")),
                state=str(p.get("state", "open")),
                draft=bool(p.get("draft", False)),
                merged=bool(p.get("merged", False)),
                head_sha=str(p.get("head_sha", "")),
                url=str(p.get("url", "")),
                author=str(p.get("author", "")),
                source=label,
            )
            for p in raw.get("pulls", [])
        ]

    def get(self, number: int) -> PullSummary | None:
        return next((p for p in self._pulls if p.number == number), None)

    def list(self, *, state: str = "open", base: str | None = None) -> list[PullSummary]:
        return [
            p
            for p in sorted(self._pulls, key=lambda p: -p.number)
            if (state == "all" or p.state == state) and (base is None or p.base_ref == base)
        ]


@dataclass(frozen=True)
class PrRef:
    number: int
    repository: str | None  # from a URL; None for "#12" / "12"


def parse_pr_ref(value: str) -> PrRef:
    text = value.strip()
    match = _PR_URL.match(text)
    if match:
        return PrRef(int(match.group(3)), match.group(2))
    if re.fullmatch(r"#?\d{1,9}", text):
        return PrRef(int(text.lstrip("#")), None)
    raise InvalidInputError(f"Not a PR number or URL: {value!r} (use 12, #12 or a /pull/12 URL)")


def extract_keys(pull: PullSummary, pattern: str) -> dict[str, list[str]]:
    """Ticket keys by location: ``title``, ``branch``, ``body`` (each list de-duplicated)."""
    regex = re.compile(pattern)
    return {
        "title": list(dict.fromkeys(regex.findall(pull.title))),
        "branch": list(dict.fromkeys(regex.findall(pull.head_ref))),
        "body": list(dict.fromkeys(regex.findall(pull.body))),
    }


def own_keys(locations: dict[str, list[str]]) -> list[str]:
    """The PR's own tickets: title and branch keys (body keys are mentions only)."""
    return list(dict.fromkeys(locations["title"] + locations["branch"]))


def _pull_view(p: PullSummary, pattern: str) -> dict[str, Any]:
    loc = extract_keys(p, pattern)
    return {
        "number": p.number,
        "title": p.title,
        "head_ref": p.head_ref,
        "base_ref": p.base_ref,
        "state": "merged" if p.merged else p.state,
        "draft": p.draft,
        "url": p.url,
        "tickets": own_keys(loc),
        "mentions": [k for k in loc["body"] if k not in own_keys(loc)],
        "source": p.source,
    }


class ChangeResolver:
    def __init__(
        self,
        *,
        git: Git,
        key_pattern: str,
        pulls: PullSource | None,
        tracker: IssueTracker,
        discovery: dict[str, Any],
        repository: str | None,
        default_branch: str | None,
    ) -> None:
        self.git, self.pattern, self.pulls = git, key_pattern, pulls
        self.tracker, self.disc = tracker, discovery
        self.repository, self.default_branch = repository, default_branch
        self._out: dict[str, Any] = {}

    def _list(self, *, state: str, base: str | None = None) -> list[PullSummary] | None:
        """PR listing that degrades: an unreachable source is reported, never fatal, and
        never read as "no PRs" (ADR-0017, Scenario K)."""
        if self.pulls is None:
            return None
        try:
            return self.pulls.list(state=state, base=base)
        except OrchestratorError as exc:
            note = f"PR evidence unavailable: {exc.message}"
            if self._out and note not in self._out["warnings"]:
                self._out["warnings"].append(note)
                self._out["sources"]["pull_requests"] = f"unavailable ({exc.code})"
            return None

    # ------------------------------------------------------------------ entry point
    def resolve(self, *, pr: str | None = None, ticket: str | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {
            "scenario": {
                (True, False): "A:pr",
                (False, True): "B:ticket",
                (True, True): "C:both",
            }.get((pr is not None, ticket is not None), "D:discover"),
            "status": RESOLVED,
            "repository": self.repository,
            "pull_request": None,
            "tickets": [],
            "questions": [],
            "conflicts": [],
            "warnings": [],
            "sources": {
                "pull_requests": self.pulls.source if self.pulls else "unavailable",
                "issues": "not configured"
                if isinstance(self.tracker, NullIssueTracker)
                else f"issue snapshot (fetched_at {self.tracker.fetched_at()})",
            },
        }
        self._out = out
        if ticket is not None and not re.fullmatch(self.pattern, ticket):
            raise InvalidInputError(f"{ticket!r} doesn't match the change-key pattern")
        pull = self._load_pull(pr, out) if pr is not None else None
        if pr is not None and pull is None:
            return out
        if pull is not None:
            view = _pull_view(pull, self.pattern)
            out["pull_request"] = view
            if ticket is None:
                if not view["tickets"]:
                    hint = (
                        f"; its body mentions {', '.join(view['mentions'])}"
                        if view["mentions"]
                        else ""
                    )
                    self._ask(
                        out,
                        "ticket",
                        f"PR #{pull.number} has no ticket key in its title or branch{hint}. "
                        "Which Jira ticket does it implement?",
                        view["mentions"],
                    )
                    return out
                out["tickets"] = view["tickets"]
                if len(view["tickets"]) > 1:
                    out["warnings"].append(
                        f"PR #{pull.number} carries several tickets: {', '.join(view['tickets'])}"
                    )
            else:
                out["tickets"] = [ticket]
                if ticket not in view["tickets"]:
                    out["status"] = CONFLICT
                    out["conflicts"].append(
                        f"PR #{pull.number} is about {', '.join(view['tickets']) or 'no ticket'}"
                        f" (title/branch), not {ticket}"
                        + (" (only mentioned in its body)" if ticket in view["mentions"] else "")
                    )
                    self._ask(
                        out,
                        "confirm-link",
                        f"PR #{pull.number} and {ticket} don't match. Which is right?",
                        [f"#{pull.number}", ticket],
                    )
                issue = self.tracker.get(ticket)
                if issue and issue.pull_requests and pull.number not in issue.pull_requests:
                    out["conflicts"].append(
                        f"Jira {ticket} links PR(s) {list(issue.pull_requests)}, not #{pull.number}"
                    )
                    out["status"] = CONFLICT
        elif ticket is not None:
            out["tickets"] = [ticket]
        else:
            self._discover_open(out)
            return out
        out["changes"] = [self._change(key, out) for key in out["tickets"]]
        if ticket is not None and pull is None:
            linked = out["changes"][0]["pull_requests"]
            if not linked:
                open_prs = self._open_without_ticket()
                self._ask(
                    out,
                    "pull-request",
                    f"No PR found for {ticket} (title/branch convention or Jira link). "
                    "Which PR implements it?",
                    [f"#{p['number']} {p['title']}" for p in open_prs],
                    soft=True,
                )
        return out

    # ------------------------------------------------------------------ helpers
    def _ask(
        self, out: dict[str, Any], qid: str, text: str, options: list[str], soft: bool = False
    ) -> None:
        out["questions"].append({"id": qid, "question": text, "options": options})
        if not soft and out["status"] == RESOLVED:
            out["status"] = NEEDS_INPUT
        if soft:
            out["warnings"].append(text)

    def _load_pull(self, pr: str, out: dict[str, Any]) -> PullSummary | None:
        ref = parse_pr_ref(pr)
        if ref.repository and self.repository and ref.repository.lower() != self.repository.lower():
            out["status"] = CONFLICT
            out["conflicts"].append(
                f"PR URL is for {ref.repository}, but this repository is {self.repository}"
            )
            self._ask(out, "repository", "Use the PR from the other repository?", [])
            return None
        if self.pulls is None:
            self._ask(
                out,
                "pull-request-source",
                "PRs can't be read (no GitHub access and no PR snapshot). Which ticket?",
                [],
            )
            return None
        try:
            pull = self.pulls.get(ref.number)
        except OrchestratorError as exc:
            self._ask(out, "pull-request", f"PR #{ref.number} unreadable: {exc.message}", [])
            return None
        if pull is None:
            self._ask(out, "pull-request", f"PR #{ref.number} was not found", [])
        return pull

    def _search(self, key: str) -> list[dict[str, Any]]:
        pulls = self._list(state="all")
        if pulls is None:
            return []
        views = [_pull_view(p, self.pattern) for p in pulls]
        return [v for v in views if key in v["tickets"]]

    def _open_without_ticket(self) -> list[dict[str, Any]]:
        pulls = self._list(state="open", base=self.default_branch)
        views = [_pull_view(p, self.pattern) for p in pulls or []]
        return [v for v in views if not v["tickets"]]

    def _released_in(self, key: str) -> str | None:
        stable = self.disc.get("latest_stable")
        if not stable or not re.fullmatch(r"[A-Za-z0-9_-]+", key):
            return None
        # keys are validated against [A-Z0-9-] shapes, so they are literal in ERE
        out = self.git.first_commit_matching(stable["sha"], f"(^|[^A-Z0-9-]){key}([^0-9]|$)")
        return stable["tag"] if out else None

    def _change(self, key: str, out: dict[str, Any]) -> dict[str, Any]:
        issue = self.tracker.get(key)
        pending = {c["key"]: c for c in self.disc.get("pending_changes", [])}
        by_search = self._search(key)
        prs = {p["number"]: p for p in by_search}
        if issue:
            for number in issue.pull_requests:
                if number not in prs and self.pulls is not None:
                    try:
                        pull = self.pulls.get(number)
                    except OrchestratorError:
                        pull = None
                    if pull is not None:
                        prs[number] = {**_pull_view(pull, self.pattern), "linked_by": "jira"}
        prerequisites = []
        for dep in issue.blocked_by if issue else ():
            dep_issue = self.tracker.get(dep)
            released = self._released_in(dep)
            in_rcs = pending[dep]["in_candidates"] if dep in pending else []
            state = (
                "released"
                if released
                else "in-candidate"
                if in_rcs
                else "pending"
                if dep in pending
                else "missing"
            )
            prerequisites.append(
                {
                    "key": dep,
                    "evidence": f"Jira: {key} is blocked by {dep}",
                    "classification": "CONFIRMED",
                    "issue_status": dep_issue.status if dep_issue else None,
                    "released_in": released,
                    "in_candidates": in_rcs,
                    "state": state,
                    "pull_requests": [p["number"] for p in self._search(dep)],
                }
            )
            if state == "missing":
                out["warnings"].append(
                    f"{key} is blocked by {dep}, which is neither released nor on "
                    f"{self.disc.get('source', {}).get('ref', 'the source branch')}"
                )
        if issue is None:
            out["warnings"].append(
                f"{key} is not in the issue snapshot; its status and links are unknown"
            )
        return {
            "key": key,
            "issue": {
                "summary": issue.summary,
                "status": issue.status,
                "blocked_by": list(issue.blocked_by),
                "accessible": issue.accessible,
                "linked_pull_requests": list(issue.pull_requests),
            }
            if issue
            else None,
            "pull_requests": sorted(prs.values(), key=lambda p: p["number"]),
            "pending": key in pending,
            "released_in": None if key in pending else self._released_in(key),
            "in_candidates": pending[key]["in_candidates"] if key in pending else [],
            "prerequisites": prerequisites,
            # every prerequisite not yet in a stable release must ship with or before it
            "missing_prerequisites": [p["key"] for p in prerequisites if p["state"] != "released"],
        }

    def _discover_open(self, out: dict[str, Any]) -> None:
        pulls = self._list(state="open", base=self.default_branch)
        if pulls is None:
            reason = "" if self.pulls is None else " (the PR list couldn't be read)"
            self._ask(out, "ticket", f"Which ticket or PR should be released?{reason}", [])
            return
        views = [_pull_view(p, self.pattern) for p in pulls]
        out["candidates"] = views
        if not views:
            self._ask(
                out,
                "ticket",
                f"No open PRs target {self.default_branch}. Which ticket or PR?",
                [],
            )
            return
        self._ask(
            out,
            "pull-request",
            f"{len(views)} open PR(s) target {self.default_branch}. Which one?",
            [
                f"#{v['number']} {v['title']}" + ("" if v["tickets"] else " (no ticket key)")
                for v in views
            ],
        )
