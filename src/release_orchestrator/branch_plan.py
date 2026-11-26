"""Branch and PR strategy before any library or consumer PR is prepared (ADR-0017).

For every repository that would receive a branch (the library and each consumer
selected for an update), this module reports, read-only:

* the **ticket-linked branches** that already exist: local, remote-tracking, and GitHub
  (MCP ``list_branches`` evidence). A branch matches only when the ticket key appears as
  a whole token (``feature/FIN-204-x`` matches FIN-204; ``FIN-2040`` doesn't);
* their state: ``merged`` (tip reachable from the target), ``active`` (commits within
  30 days), ``stale``, or ``unverified`` (seen on GitHub, not fetched locally);
* existing PRs for them, and for the deterministic update branch (duplicates);
* the proposal: source branch, target branch, and the strategy:

  ============  ==============================================================
  new           no matching branch: a deterministic branch from the target
  stack         one active feature branch: a *new* update branch on its tip, with a
                Draft PR into the feature branch (the feature branch isn't modified)
  separate      the alternative to ``stack``: a new branch from the default branch
  ambiguous     several candidates: the engineer must choose; nothing is assumed
  existing      the update branch already exists (idempotent re-run)
  ============  ==============================================================

Nothing here creates a branch. Execution stays in the existing ``--approve`` commands,
with the confirmed target passed explicitly (``update-consumers --base svc=<branch>``).
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from release_orchestrator.config import Config
from release_orchestrator.consumers.updater import branch_name
from release_orchestrator.errors import InvalidInputError
from release_orchestrator.gitutil.repo import Git
from release_orchestrator.integrations.mcp_snapshot import McpStore
from release_orchestrator.planning.plan import ReleasePlan
from release_orchestrator.workflow import integration_branch

ACTIVE_DAYS = 30


@dataclass
class BranchRef:
    name: str
    where: str  # local | remote-tracking | github
    sha: str | None
    state: str  # merged | active | stale | unverified
    pull_requests: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class RepoPlan:
    repository: str
    kind: str  # library | consumer
    tickets: list[str]
    target: str
    proposed_source: str
    strategy: str
    existing_branches: list[BranchRef] = field(default_factory=list)
    duplicate_prs: list[dict[str, Any]] = field(default_factory=list)
    alternatives: list[dict[str, str]] = field(default_factory=list)
    intended_changes: str = ""
    needs_confirmation: bool = True
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def ticket_token(key: str) -> re.Pattern[str]:
    return re.compile(rf"(?i)(?:^|[^a-z0-9]){re.escape(key)}(?:[^0-9]|$)")


def _refs(git: Git) -> list[tuple[str, str, str]]:
    out = git.out(
        "for-each-ref",
        "--format=%(refname)%00%(objectname)",
        "refs/heads",
        "refs/remotes",
    )
    refs = []
    for line in out.splitlines():
        name, _, sha = line.partition("\x00")
        if name.endswith("/HEAD"):
            continue
        if name.startswith("refs/heads/"):
            refs.append((name[len("refs/heads/") :], "local", sha))
        elif name.startswith("refs/remotes/"):
            refs.append((name.split("/", 3)[-1], "remote-tracking", sha))
    return refs


def _state(git: Git, sha: str, target_sha: str | None) -> str:
    if target_sha and git.is_ancestor(sha, target_sha):
        return "merged"
    stamp = git.out("log", "-1", "--format=%ct", sha)
    age_days = (time.time() - int(stamp or 0)) / 86400
    return "active" if age_days <= ACTIVE_DAYS else "stale"


def find_branches(
    git: Git,
    tickets: list[str],
    *,
    target: str,
    github: list[dict[str, Any]] | None = None,
    pulls: list[dict[str, Any]] | None = None,
) -> list[BranchRef]:
    patterns = [ticket_token(t) for t in tickets]
    target_sha = git.try_rev_parse(target)
    found: dict[str, BranchRef] = {}
    for name, where, sha in _refs(git):
        if name == target or not any(p.search(name) for p in patterns):
            continue
        if name in found:
            continue  # the local copy wins over its remote-tracking twin
        found[name] = BranchRef(name, where, sha, _state(git, sha, target_sha))
    for entry in github or []:
        gh_name: str = str(entry["name"])
        gh_sha: str | None = entry.get("sha")
        if gh_name in found or gh_name == target or not any(p.search(gh_name) for p in patterns):
            continue
        local = git.try_rev_parse(gh_sha) if gh_sha else None
        state = _state(git, local, target_sha) if local else "unverified"
        found[gh_name] = BranchRef(gh_name, "github", gh_sha, state)
    for ref in found.values():
        ref.pull_requests = [
            {
                "number": p["number"],
                "state": "merged" if p["merged"] else p["state"],
                "base": p["base_ref"],
                "url": p["url"],
            }
            for p in pulls or []
            if p["head_ref"] == ref.name
        ]
    return sorted(found.values(), key=lambda r: r.name)


def _github_evidence(store: McpStore | None, repository: str | None) -> tuple[Any, Any]:
    if store is None or not repository:
        return None, None
    branches = store.get("github-branches", repository)
    pulls = store.get("github-pulls", repository)
    return (branches.data if branches else None), (pulls.data if pulls else None)


def plan_branches(
    plan: ReleasePlan,
    config: Config,
    *,
    selected: list[str] | None = None,
    store: McpStore | None = None,
    repositories: dict[str, str] | None = None,
) -> dict[str, Any]:
    tickets = plan.included_keys
    repositories = repositories or {}
    out: list[RepoPlan] = []

    lib_git = Git(config.library.repo)
    gh_branches, gh_pulls = _github_evidence(store, repositories.get(config.library.name))
    lib = RepoPlan(
        repository=config.library.name,
        kind="library",
        tickets=tickets,
        target=integration_branch(config, plan) or config.library.source_ref,
        proposed_source=plan.branch,
        strategy="existing" if lib_git.branch_exists(plan.branch) else "new",
        existing_branches=find_branches(
            lib_git,
            tickets,
            target=config.library.source_ref,
            github=gh_branches,
            pulls=gh_pulls,
        ),
        intended_changes=f"release branch {plan.branch}: {', '.join(tickets)} cherry-picked "
        f"onto {plan.base_ref}, version {plan.proposed_version}",
        notes=[
            "feature branches of included tickets are context only: the release branch is "
            "built from the plan, never from a feature branch"
        ],
    )
    lib.duplicate_prs = [p for p in gh_pulls or [] if p["head_ref"] == plan.branch]
    out.append(lib)

    for c in plan.consumers:
        if c["action"] != "update" or (selected is not None and c["name"] not in selected):
            continue
        consumer = config.consumer(c["name"])
        git = Git(consumer.repo)
        update = branch_name(consumer, plan.library, plan.proposed_version)
        gh_branches, gh_pulls = _github_evidence(store, repositories.get(consumer.name))
        branches = find_branches(
            git, tickets, target=consumer.source_ref, github=gh_branches, pulls=gh_pulls
        )
        live = [b for b in branches if b.state in ("active", "unverified")]
        rp = RepoPlan(
            repository=consumer.name,
            kind="consumer",
            tickets=tickets,
            target=consumer.source_ref,
            proposed_source=update,
            strategy="new",
            existing_branches=branches,
            duplicate_prs=[p for p in gh_pulls or [] if p["head_ref"] == update],
            intended_changes=f"{consumer.dependency_file}: {plan.library} "
            f"{c.get('current_version')} → {plan.proposed_version} (nothing else)",
        )
        if git.branch_exists(update):
            rp.strategy = "existing"
            rp.notes.append(f"{update} already exists: a re-run verifies it, never rewrites it")
        elif len(live) == 1:
            feature = live[0]
            rp.strategy = "stack"
            rp.target = feature.name
            rp.alternatives = [
                {
                    "strategy": "stack",
                    "target": feature.name,
                    "effect": f"{update} is created on top of {feature.name}; its Draft PR "
                    f"targets {feature.name}; the feature branch itself isn't modified",
                },
                {
                    "strategy": "separate",
                    "target": consumer.source_ref,
                    "effect": f"{update} is created from {consumer.source_ref}, independent "
                    "of the feature branch",
                },
            ]
            if feature.state == "unverified":
                rp.notes.append(
                    f"{feature.name} exists on GitHub but isn't fetched locally: fetch it "
                    "before stacking, or choose 'separate'"
                )
        elif len(live) > 1:
            rp.strategy = "ambiguous"
            rp.alternatives = [
                {"strategy": "stack", "target": b.name, "effect": f"stack on {b.name}"}
                for b in live
            ] + [
                {
                    "strategy": "separate",
                    "target": consumer.source_ref,
                    "effect": f"from {consumer.source_ref}",
                }
            ]
            rp.notes.append("several ticket-linked branches are active: choose one explicitly")
        if rp.duplicate_prs:
            rp.notes.append(
                f"a PR already exists for {update}: reconcile it, never open a second one"
            )
        out.append(rp)

    return {
        "plan_id": plan.plan_id,
        "repositories": [r.to_dict() for r in out],
        "confirm": "approve each repository's source/target before any branch is created; "
        "pass the target explicitly: update-consumers --base <service>=<branch>",
    }


def resolve_base(config: Config, service: str, bases: dict[str, str]) -> str:
    """The confirmed target for a consumer (default: its configured source branch)."""
    return bases.get(service, config.consumer(service).source_ref)


def parse_bases(values: list[str]) -> dict[str, str]:
    bases = {}
    for item in values:
        service, sep, ref = item.partition("=")
        if not sep or not service.strip() or not ref.strip():
            raise InvalidInputError(f"--base must be <service>=<branch>, got {item!r}")
        bases[service.strip()] = ref.strip()
    return bases
