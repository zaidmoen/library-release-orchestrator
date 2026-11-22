"""``release-orchestrator`` command-line interface.

stdout carries exactly one result: a JSON envelope (``--json``) or a concise text summary.
Logs go to stderr. Exit codes are stable (see ``errors.ExitCode`` and docs/cli.md).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from release_orchestrator import __version__
from release_orchestrator import cache as disk_cache
from release_orchestrator.assess import assess, render_brief
from release_orchestrator.config import load_config
from release_orchestrator.context import RepoContext, discover_context
from release_orchestrator.demo import SCENARIOS, build_demo
from release_orchestrator.environments import EnvironmentLedger
from release_orchestrator.errors import (
    BlockedError,
    ConfigError,
    ExitCode,
    InvalidInputError,
    OrchestratorError,
)
from release_orchestrator.gitutil.repo import NETWORK, STATS, git_session, read_snapshot
from release_orchestrator.integrations.dependency_sheet import SheetSnapshot, build_sheet
from release_orchestrator.integrations.github_status import (
    ApprovalPolicy,
    GitHubReader,
    evaluate_approval,
)
from release_orchestrator.integrations.linkage import (
    ChangeResolver,
    GitHubPullSource,
    PullSource,
    SnapshotPullSource,
)
from release_orchestrator.lab import build_lab
from release_orchestrator.modes import MODES, ModeEngine, render_preview
from release_orchestrator.planning.manifest import load_plan
from release_orchestrator.release_graph import build_release_graph
from release_orchestrator.reporting.release_report import (
    build_report,
    render_markdown,
    render_slack,
    render_slack_text,
)
from release_orchestrator.workflow import Orchestrator, RequestInput, Services, write_plan

# Commands that never write to a repository ref, registry, environment or remote.
READ_ONLY_COMMANDS = {"release-mode", "resolve-change", "release-graph", "release-report", "assess"}
WRITE_COMMANDS = {"prepare", "pr", "publish", "update-consumers", "push", "promote", "rollback"}


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {"level": record.levelname, "logger": record.name, "message": record.getMessage()}
        for key in (
            "argv",
            "plan_id",
            "from_state",
            "to_state",
            "note",
            "version",
            "attempt",
            "delay",
        ):
            if hasattr(record, key):
                data[key] = getattr(record, key)
        return json.dumps(data)


def _configure_logging(verbose: bool, json_logs: bool) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        _JsonFormatter() if json_logs else logging.Formatter("%(levelname)s %(name)s: %(message)s")
    )
    root = logging.getLogger("release_orchestrator")
    root.handlers[:] = [handler]
    root.setLevel(logging.DEBUG if verbose else logging.WARNING)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="release-orchestrator",
        description="Dependency-aware, out-of-order release planning for shared libraries.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="trusted config file (default: $LRO_CONFIG, ./release-orchestrator.yaml, or the "
        "nearest config describing the current repository)",
    )
    parser.add_argument(
        "--repository",
        dest="repository_override",
        help="GitHub owner/name override when it can't be discovered from the Git remote",
    )
    parser.add_argument(
        "--no-cache", action="store_true", help="don't use the immutable analysis cache"
    )
    parser.add_argument("--json", action="store_true", help="emit a JSON envelope on stdout")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--log-json", action="store_true", help="structured JSON logs on stderr")
    sub = parser.add_subparsers(dest="command", required=True)

    def request_args(p: argparse.ArgumentParser) -> None:
        group = p.add_mutually_exclusive_group(required=True)
        group.add_argument("--ticket", action="append", default=[], help="change key (repeatable)")
        group.add_argument("--request", help="natural-language request")
        group.add_argument("--commit", action="append", default=[], help="commit SHA (repeatable)")
        p.add_argument("--base", help="release base (default: config base_ref or latest tag)")
        p.add_argument("--source", help="source ref (default: config source_ref); may be an RC tag")
        p.add_argument(
            "--prerelease", choices=["rc"], help="plan a release candidate, e.g. 1.2.0-rc.24"
        )

    request_args(sub.add_parser("analyze", help="read-only dependency analysis"))
    plan = sub.add_parser("plan", help="generate a deterministic release plan (no repo writes)")
    request_args(plan)
    plan.add_argument("--out", type=Path, help="write plan JSON (+ .md) to this path")

    for name, help_text in (
        ("validate-manifest", "schema-check a plan and compare it with the trusted config"),
        ("verify", "verify the release branch against the plan"),
        ("validate", "run allowlisted validation on the release branch"),
        ("status", "show run state for a plan"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--plan", type=Path, required=True)
    for name, help_text in (
        ("prepare", "build the isolated release branch"),
        ("pr", "prepare the library release PR (never merged)"),
        ("publish", "publish the immutable artifact and tag"),
        ("update-consumers", "prepare dependency-update branches/PRs for targeted consumers"),
        ("push", "push release/consumer branches and tag to allowlisted remotes (never forced)"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--plan", type=Path, required=True)
        p.add_argument("--approve", action="store_true", help="explicit human approval for writes")
        if name in ("prepare", "update-consumers"):
            p.add_argument("--dry-run", action="store_true", help="simulate without any mutation")
        if name == "update-consumers":
            p.add_argument(
                "--consumer",
                action="append",
                default=[],
                help="restrict the operation to these services (repeatable)",
            )

    lin = sub.add_parser("verify-lineage", help="final release == approved RC except version file")
    lin.add_argument("--plan", type=Path, required=True)
    lin.add_argument("--candidate", required=True, help="RC tag or version, e.g. v1.1.0-rc.22")

    rm = sub.add_parser(
        "release-mode",
        help="interactive release modes: discovery, Q1 options, impact preview (read-only)",
    )
    rm.add_argument("action", choices=["discover", "options", "preview", "check-preview"])
    rm.add_argument("--mode", choices=list(MODES))
    rm.add_argument("--ticket", action="append", default=[], help="release-candidate scope")
    rm.add_argument("--candidate", help="release mode: candidate tag/version to finalize")
    rm.add_argument("--order", help="reorder mode: comma-separated candidates, first = first")
    rm.add_argument("--choice", help="Q1 option id (from 'options')")
    rm.add_argument("--services", help="Q2: comma-separated service names, or 'all' (default all)")
    rm.add_argument("--environments", help="Q3: comma-separated envs, 'all'; omitted = none")
    rm.add_argument("--preview", type=Path, help="check-preview: preview JSON file")
    rm.add_argument("--out", type=Path, help="preview: write JSON (+ .md table) here")

    st = sub.add_parser("pr-status", help="authoritative PR state and approval decision (GitHub)")
    st.add_argument("--plan", type=Path, required=True)
    gh = sub.add_parser("github-pr-status", help="read any PR's state, reviews and checks")
    gh.add_argument("--repository", required=True, help="owner/name")
    gh.add_argument("--number", type=int, required=True)
    gh.add_argument("--expected-head", help="commit the approval must be on (default: PR head)")
    gh.add_argument("--required-approvals", type=int, default=1)
    gh.add_argument("--required-check", action="append", default=[])
    gh.add_argument("--allow-draft", action="store_true")
    gh.add_argument(
        "--min-reviewer-permission",
        default="write",
        choices=["read", "triage", "write", "maintain", "admin"],
    )

    rec = sub.add_parser("reconcile-prs", help="match PR requests with PRs that exist on GitHub")
    rec.add_argument("--plan", type=Path, required=True)

    record = sub.add_parser("record-pr", help="record a PR created externally (GitHub MCP)")
    record.add_argument("--plan", type=Path, required=True)
    record.add_argument("--repository", required=True, help="library or consumer name")
    record.add_argument("--head", required=True, help="head branch of the PR")
    record.add_argument("--number", type=int, required=True)
    record.add_argument("--url", required=True)
    record.add_argument("--state", default="open-draft")

    sub.add_parser("env-status", help="current version per environment")
    for name, help_text in (
        ("promote", "promote a published version into an environment"),
        ("rollback-plan", "plan a roll-forward to the previous artifact (read-only)"),
        ("rollback", "execute a rollback plan (appends a ledger record)"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--env", required=True)
        if name == "promote":
            p.add_argument("--version", required=True)
        if name != "rollback-plan":
            p.add_argument("--approved-by", action="append", default=[])
            p.add_argument("--approve", action="store_true")

    ctx = sub.add_parser("context", help="discover repository, branch, config and tags (read-only)")
    ctx.add_argument("--path", type=Path, default=None, help="directory to inspect (default: cwd)")

    res = sub.add_parser("resolve-change", help="PR <-> Jira resolution from one reference")
    res.add_argument("--pr", help="PR number, #number or URL")
    res.add_argument("--ticket", help="Jira key, e.g. FIN-211")

    graph = sub.add_parser("release-graph", help="release ordering and dependency graph")
    graph.add_argument("--ticket", action="append", default=None, help="selection (repeatable)")
    graph.add_argument("--order", help="requested candidate order, comma-separated")

    rep = sub.add_parser("release-report", help="dry-run report + Slack-ready summary")
    rep.add_argument("--preview", type=Path, required=True, help="preview JSON (release-mode)")
    rep.add_argument("--pr", help="source PR (number, #number or URL)")
    rep.add_argument("--ticket", help="source Jira key")
    rep.add_argument("--out", type=Path, help="directory for report.md/json and slack.json/txt")

    ass = sub.add_parser(
        "assess", help="one-call release assessment for /library-release (read-only)"
    )
    ass.add_argument("--pr", help="PR number, #number or URL")
    ass.add_argument("--ticket", help="Jira key")
    ass.add_argument("--candidate", help="release tag/version to finalize, e.g. v1.1.0-rc.22")
    ass.add_argument("--order", help="reorder: comma-separated candidates")
    ass.add_argument("--brief", action="store_true", help="print the compact card (text mode)")

    cache_cmd = sub.add_parser("cache", help="immutable analysis cache")
    cache_cmd.add_argument("action", choices=["clear", "stats"])

    lab = sub.add_parser("lab-init", help="create the release lab (library + 2 consumers)")
    lab.add_argument("--dest", type=Path, required=True)

    demo = sub.add_parser("demo-init", help="create the fictional demo environment")
    demo.add_argument("--dest", type=Path, required=True)
    demo.add_argument("--scenario", choices=SCENARIOS, default="independent")
    return parser


def run(args: argparse.Namespace) -> tuple[ExitCode, dict[str, Any]]:
    if args.command == "demo-init":
        config_path = build_demo(args.dest, args.scenario)
        return ExitCode.OK, {"config": str(config_path), "scenario": args.scenario}

    if args.command == "github-pr-status":
        evidence = GitHubReader().evidence(args.repository, args.number)
        decision = evaluate_approval(
            evidence,
            ApprovalPolicy(
                args.required_approvals,
                tuple(args.required_check),
                not args.allow_draft,
                args.min_reviewer_permission,
            ),
            expected_head=args.expected_head or evidence.pull.head_sha,
        )
        return ExitCode.OK, {"evidence": evidence.to_dict(), "decision": decision.to_dict()}

    if args.command == "lab-init":
        return ExitCode.OK, {"config": str(build_lab(args.dest))}

    if args.command == "context":
        context, _ = discover_context(
            args.path or Path.cwd(),
            config_path=_explicit_config(args),
            repository=args.repository_override,
            reader=GitHubReader(),
        )
        return (ExitCode.OK if context.ok else ExitCode.AMBIGUOUS_OR_NOT_FOUND), context.to_dict()

    config = load_config(_config_path(args))
    # ADR-0016: the immutable-analysis cache (never mutable state); --no-cache disables it.
    disk_cache.configure(disk_cache.for_state_dir(config.state_dir, enabled=not args.no_cache))
    if args.command == "cache":
        active = disk_cache.for_state_dir(config.state_dir)
        if args.action == "clear":
            return ExitCode.OK, {"removed_entries": active.clear()}
        return ExitCode.OK, active.stats()
    orch = Orchestrator(Services.from_config(config))

    if args.command in READ_ONLY_COMMANDS:
        # Refs of the user's repositories are resolved once for this read-only run.
        repos = [config.library.repo, *(c.repo for c in config.consumers)]
        with read_snapshot(repos):
            if args.command == "release-mode":
                return _release_mode(orch, args)
            if args.command == "assess":
                return _assess(orch, args)
            return _discovery_command(orch, args)

    if args.command in ("env-status", "promote", "rollback-plan", "rollback"):
        ledger = EnvironmentLedger(config, orch.s.registry)
        if args.command == "env-status":
            return ExitCode.OK, ledger.status()
        if args.command == "rollback-plan":
            return ExitCode.OK, ledger.rollback_plan(args.env)
        if args.command == "promote":
            return ExitCode.OK, ledger.promote(
                args.version, args.env, approved_by=args.approved_by, approve=args.approve
            )
        return ExitCode.OK, ledger.rollback(
            args.env, approved_by=args.approved_by, approve=args.approve
        )

    if args.command in ("analyze", "plan"):
        request = RequestInput(
            tuple(args.ticket),
            args.request,
            tuple(args.commit),
            args.base,
            args.source,
            args.prerelease,
        )
        if args.command == "analyze":
            resolved, scan, analysis = orch.analyze(request)
            code = ExitCode.BLOCKED if analysis.blockers else ExitCode.OK
            return code, {
                "request": resolved.to_dict(),
                "history": scan.to_dict(),
                "analysis": analysis.to_dict(),
            }
        try:
            plan, run_state = orch.plan(request)
        except BlockedError as exc:
            if exc.reason != "NOTHING_TO_RELEASE":
                raise
            # A re-run after a completed release is recognised, not reported as a failure.
            return ExitCode.OK, {
                "status": "ALREADY_RELEASED",
                "message": exc.message,
                **exc.details,
            }
        result: dict[str, Any] = {
            "plan_id": plan.plan_id,
            "status": plan.status,
            "state": str(run_state.state),
            "plan": plan.to_dict(),
        }
        if args.out:
            json_path, md_path = write_plan(plan, args.out)
            result["files"] = {"json": str(json_path), "markdown": str(md_path)}
        return (ExitCode.OK if plan.status == "PLAN_READY" else ExitCode.BLOCKED), result

    plan = load_plan(args.plan, config)
    if args.command == "validate-manifest":
        return ExitCode.OK, {"plan_id": plan.plan_id, "valid": True}
    if args.command == "verify":
        report = orch.verify(plan)
        return (ExitCode.OK if report["ok"] else ExitCode.BLOCKED), report
    if args.command == "status":
        return ExitCode.OK, orch.status(plan).to_dict()
    if args.command == "validate":
        return ExitCode.OK, orch.validate(plan)
    if args.command == "prepare":
        return ExitCode.OK, orch.prepare(plan, approve=args.approve, dry_run=args.dry_run)
    if args.command == "pr":
        return ExitCode.OK, orch.open_pr(plan, approve=args.approve)
    if args.command == "publish":
        return ExitCode.OK, orch.publish(plan, approve=args.approve)
    if args.command == "update-consumers":
        return ExitCode.OK, orch.update_consumers(
            plan,
            approve=args.approve,
            dry_run=args.dry_run,
            only=tuple(args.consumer) if args.consumer else None,
        )
    if args.command == "push":
        return ExitCode.OK, orch.push(plan, approve=args.approve)
    if args.command == "pr-status":
        status = orch.pr_status(plan)
        approved = status.get("decision", {}).get("approved", True)
        return (ExitCode.OK if approved else ExitCode.BLOCKED), status
    if args.command == "verify-lineage":
        return ExitCode.OK, orch.verify_lineage(plan, args.candidate)
    if args.command == "reconcile-prs":
        summary = orch.reconcile_prs(plan)
        return (ExitCode.OK if summary["ok"] else ExitCode.BLOCKED), summary
    if args.command == "record-pr":
        return ExitCode.OK, orch.record_pr(
            plan,
            repository=args.repository,
            head=args.head,
            number=args.number,
            url=args.url,
            state=args.state,
        )
    raise AssertionError(args.command)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse usage errors -> INVALID_INPUT
        return int(ExitCode.INVALID_INPUT) if exc.code not in (0, None) else 0
    _configure_logging(args.verbose, args.log_json)
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:  # cards use emoji; Windows consoles default to cp1252
        with contextlib.suppress(ValueError, OSError):
            reconfigure(encoding="utf-8", errors="replace")
    error: dict[str, Any] | None = None
    result: dict[str, Any] = {}
    try:
        with git_session():  # every cat-file reader is closed when the command ends
            code, result = run(args)
    except OrchestratorError as exc:
        code, error = exc.exit_code, exc.to_dict()
    except Exception as exc:
        logging.getLogger("release_orchestrator").exception("internal error")
        code, error = ExitCode.INTERNAL_ERROR, {"code": "INTERNAL_ERROR", "message": str(exc)}

    envelope = {
        "ok": code == ExitCode.OK,
        "command": args.command,
        "exit_code": int(code),
        "result": result,
        "error": error,
    }
    if args.json:
        print(json.dumps(envelope, indent=2, default=str))
    elif args.command == "assess" and getattr(args, "brief", False) and result and not error:
        print(render_brief(result), end="")
    else:
        print(_text_summary(args.command, envelope))
    return int(code)


def _text_summary(command: str, env: dict[str, Any]) -> str:
    if env["error"]:
        err = env["error"]
        reason = f" [{err['reason']}]" if err.get("reason") else ""
        lines = [f"{command}: {err['code']}{reason}: {err['message']}"]
        details = err.get("details") or {}
        for key in ("errors", "conflicted_files", "push_command", "blockers"):
            if key in details:
                lines.append(f"  {key}: {json.dumps(details[key], default=str)[:600]}")
        if err["code"] == "APPROVAL_REQUIRED":
            lines.append("  Re-run with --approve once a human has reviewed the plan.")
        return "\n".join(lines)
    res = env["result"]
    if command == "plan" and res.get("status") == "ALREADY_RELEASED":
        return f"plan: ALREADY_RELEASED: {res['message']} ({res['released_in_revision'][:10]})"
    if command in ("plan",):
        plan = res["plan"]
        lines = [
            f"plan {res['plan_id']}: {res['status']} (run state {res['state']})",
            f"  version: {plan['release']['previous_version']} -> "
            f"{plan['release']['proposed_version']} ({plan['release']['bump']})",
            f"  verdict: {plan['analysis']['verdict']}",
            f"  include: {[c['key'] for c in plan['included_changes']]}",
            f"  exclude: {[c['key'] for c in plan['excluded_changes']]}",
        ]
        lines += [f"  BLOCKER {b['reason']}: {b['message']}" for b in plan["blockers"]]
        lines += [f"  consumer {c['name']}: {c['action']}" for c in plan["consumers"]]
        if res.get("files"):
            lines.append(f"  written: {res['files']['json']}")
        return "\n".join(lines)
    if command == "analyze":
        analysis = res["analysis"]
        lines = [f"verdict: {analysis['verdict']}"]
        lines += [f"  {f['severity']} {f['kind']}: {f['message']}" for f in analysis["findings"]]
        return "\n".join(lines)
    return f"{command}: ok\n{json.dumps(res, indent=2, default=str)[:2000]}"


if __name__ == "__main__":
    raise SystemExit(main())


def _split(value: str | None) -> list[str] | None:
    return None if value is None else [v for v in value.split(",") if v.strip()]


def _release_mode(orch: Orchestrator, args: argparse.Namespace) -> tuple[ExitCode, dict[str, Any]]:
    engine = ModeEngine(orch)
    if args.action == "discover":
        return ExitCode.OK, engine.discover()
    if args.action == "check-preview":
        if not args.preview:
            raise InvalidInputError("check-preview needs --preview FILE")
        preview = json.loads(args.preview.read_text(encoding="utf-8"))
        return ExitCode.OK, engine.check_preview(preview)
    if not args.mode:
        raise InvalidInputError("--mode is required for options and preview")
    common = {
        "tickets": args.ticket,
        "candidate": args.candidate,
        "order": _split(args.order) or [],
    }
    if args.action == "options":
        return ExitCode.OK, engine.options(args.mode, **common)
    if not args.choice:
        raise InvalidInputError("preview needs --choice (an option id from 'options')")
    preview = engine.preview(
        args.mode,
        choice=args.choice,
        services=_split(args.services),
        environments=_split(args.environments),
        **common,
    )
    if args.out:
        args.out.write_text(json.dumps(preview, indent=2), encoding="utf-8")
        args.out.with_suffix(".md").write_text(render_preview(preview), encoding="utf-8")
    code = ExitCode.BLOCKED if preview["readiness"] == "BLOCKED" else ExitCode.OK
    return code, preview


# --------------------------------------------------------------------------- discovery (ADR-0015)
def _explicit_config(args: argparse.Namespace) -> Path | None:
    if args.config is not None:
        return Path(args.config)
    env = os.environ.get("LRO_CONFIG")
    return Path(env) if env else None


def _config_path(args: argparse.Namespace) -> Path:
    """Explicit config, else ./release-orchestrator.yaml, else the nearest config that
    describes the repository the command runs in (never one for another repository)."""
    explicit = _explicit_config(args)
    if explicit is not None:
        return explicit
    local = Path("release-orchestrator.yaml")
    if local.is_file():
        return local
    # No network here: locating the config only needs local Git (ADR-0016).
    context, config = discover_context(Path.cwd(), repository=args.repository_override)
    args.discovered_context = context  # reused by the same read-only run (no second scan)
    if config is None:
        problems = [p.to_dict() for p in context.problems]
        raise ConfigError(
            "No release config describes this repository; pass --config <file>",
            details={"problems": problems, "searched_from": context.cwd},
        )
    return config.config_path


def _context_for(orch: Orchestrator, args: argparse.Namespace) -> RepoContext:
    cached: RepoContext | None = getattr(args, "discovered_context", None)
    needs_remote_default = getattr(args, "pr", None) is None  # open-PR lookups need it
    if (
        cached is not None
        and cached.config_path == str(orch.config.config_path)
        and (not needs_remote_default or cached.default_branch_source.startswith("git"))
    ):
        return cached
    context, _ = discover_context(
        orch.config.library.repo,
        config_path=orch.config.config_path,
        repository=args.repository_override,
        reader=orch.s.github,
    )
    return context


def _pull_source(orch: Orchestrator, context: RepoContext) -> PullSource | None:
    if orch.config.pull_request_snapshot is not None:
        return SnapshotPullSource(orch.config.pull_request_snapshot)
    if context.repository and context.host == "github.com":
        return GitHubPullSource(orch.s.github, context.repository)
    return None


def _sheet(config: Any) -> SheetSnapshot | None:
    if config.dependency_sheet is None:
        return None
    return build_sheet(
        config.dependency_sheet,
        base_dir=config.config_path.parent,
        key_pattern=config.change_key_pattern,
    ).snapshot()


def _discovery_command(
    orch: Orchestrator, args: argparse.Namespace
) -> tuple[ExitCode, dict[str, Any]]:
    engine = ModeEngine(orch)
    context = _context_for(orch, args)
    if args.command == "release-graph":
        graph = build_release_graph(
            engine, sheet=_sheet(orch.config), selection=args.ticket, order=_split(args.order)
        )
        code = ExitCode.BLOCKED if graph["eligibility"] == "BLOCKED" else ExitCode.OK
        return code, {"context": context.to_dict(), "graph": graph}

    linkage = None
    if args.command == "resolve-change" or args.pr or args.ticket:
        resolver = ChangeResolver(
            git=orch.s.git,
            key_pattern=orch.config.change_key_pattern,
            pulls=_pull_source(orch, context),
            tracker=orch.s.tracker,
            discovery=engine.discover(),
            repository=context.repository,
            default_branch=context.default_branch,
        )
        linkage = resolver.resolve(pr=args.pr, ticket=args.ticket)
    if args.command == "resolve-change" and linkage is not None:
        code = ExitCode.OK if linkage["status"] == "RESOLVED" else ExitCode.AMBIGUOUS_OR_NOT_FOUND
        return code, {"context": context.to_dict(), "resolution": linkage}

    preview = json.loads(args.preview.read_text(encoding="utf-8"))
    engine.check_preview(preview)  # a stale preview can't be reported as current
    selection = sorted({k for s in preview["release_steps"] for k in s["tickets"]})
    order = (
        [s["candidate"] for s in preview["release_steps"] if s["candidate"]]
        if preview["mode"] == "reorder-releases"
        else None
    )
    graph = build_release_graph(engine, sheet=_sheet(orch.config), selection=selection, order=order)
    report = build_report(
        context=context.to_dict(),
        preview=preview,
        graph=graph,
        linkage=linkage,
        discovery=engine.discover(),
    )
    result: dict[str, Any] = {"report": report, "slack": render_slack(report)}
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        files = {
            "report.json": json.dumps(report, indent=2, default=str),
            "report.md": render_markdown(report),
            "slack.json": json.dumps(result["slack"], indent=2),
            "slack.txt": render_slack_text(report),
        }
        for name, text in files.items():
            (args.out / name).write_text(text, encoding="utf-8")
        result["files"] = {name: str(args.out / name) for name in files}
    code = ExitCode.BLOCKED if report["eligibility"] == "BLOCKED" else ExitCode.OK
    return code, result


def _assess(orch: Orchestrator, args: argparse.Namespace) -> tuple[ExitCode, dict[str, Any]]:
    """The single call behind ``/library-release`` (ADR-0016)."""
    engine = ModeEngine(orch)
    context = _context_for(orch, args)
    payload = assess(
        engine,
        context,
        pulls=_pull_source(orch, context),
        pr=args.pr,
        ticket=args.ticket,
        candidate=args.candidate,
        order=_split(args.order),
        sheet=_sheet(orch.config),
    )
    payload["cache"] = disk_cache.active().stats()
    payload["process"] = {  # whole process, including config/context discovery
        "git_subprocess_calls": STATS.calls,
        "git_seconds": round(STATS.seconds - NETWORK.git_seconds, 3),
        "network_calls": NETWORK.calls,
        "network_seconds": round(NETWORK.seconds, 3),
    }
    payload["brief"] = render_brief(payload)
    code = {
        "READY": ExitCode.OK,
        "NEEDS REVIEW": ExitCode.OK,
        "BLOCKED": ExitCode.BLOCKED,
        "UNKNOWN": ExitCode.AMBIGUOUS_OR_NOT_FOUND,
    }[payload["status"]]
    return code, payload
