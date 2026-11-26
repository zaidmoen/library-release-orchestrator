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
from release_orchestrator.bootstrap import Bootstrap, bootstrap, trust_validation
from release_orchestrator.branch_plan import parse_bases, plan_branches
from release_orchestrator.config import Config, load_config
from release_orchestrator.consumers.impact import impact_matrix
from release_orchestrator.consumers.readme_discovery import discover_consumers
from release_orchestrator.context import (
    RepoContext,
    discover_context,
    parse_remote,
    validate_repository,
)
from release_orchestrator.demo import SCENARIOS, build_demo
from release_orchestrator.environments import EnvironmentLedger
from release_orchestrator.errors import (
    BlockedError,
    ConfigError,
    ExitCode,
    InvalidInputError,
    OrchestratorError,
)
from release_orchestrator.gitutil.repo import NETWORK, STATS, Git, git_session, read_snapshot
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
from release_orchestrator.integrations.mcp_snapshot import KINDS as MCP_KINDS
from release_orchestrator.integrations.mcp_snapshot import McpPullSource, McpStore
from release_orchestrator.lab import build_lab
from release_orchestrator.modes import MODES, ModeEngine, render_preview
from release_orchestrator.notify import Notifier, load_policy, resolve_channel, set_policy
from release_orchestrator.operations import OperationStore, render_report
from release_orchestrator.planning.manifest import load_plan
from release_orchestrator.release_graph import build_release_graph
from release_orchestrator.reporting.release_notes import (
    GitHubReleaseWriter,
    build_notes,
    publish_notes,
    render_notes,
)
from release_orchestrator.reporting.release_report import (
    build_report,
    render_markdown,
    render_slack,
    render_slack_text,
)
from release_orchestrator.workflow import Orchestrator, RequestInput, Services, write_plan

# Commands that never write to a repository ref, registry, environment or remote.
READ_ONLY_COMMANDS = {
    "release-mode",
    "resolve-change",
    "release-graph",
    "release-report",
    "assess",
    "bootstrap",
    "consumers",
    "impact",
    "branch-plan",
    "release-notes",
}
# ADR-0017 commands (read-only except the explicit, local mcp-ingest store)
MCP_COMMANDS = {"bootstrap", "consumers", "impact", "branch-plan", "release-notes"}
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
            p.add_argument(
                "--base",
                action="append",
                default=[],
                help="confirmed target branch per service: <service>=<branch> (repeatable)",
            )

    lin = sub.add_parser("verify-lineage", help="final release == approved RC except version file")
    lin.add_argument("--plan", type=Path, required=True)
    lin.add_argument("--candidate", required=True, help="RC tag or version, e.g. v1.1.0-rc.22")

    rm = sub.add_parser(
        "release-mode",
        help="interactive release modes: discovery, Q1 options, impact preview (read-only)",
    )
    rm.add_argument(
        "action", choices=["discover", "options", "preview", "check-preview", "progress"]
    )
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

    ing = sub.add_parser("mcp-ingest", help="store GitHub/Jira MCP tool output as evidence")
    ing.add_argument("--kind", required=True, choices=list(MCP_KINDS))
    ing.add_argument("--repository", help="owner/name (GitHub kinds)")
    ing.add_argument("--key", default="", help="file path for github-file")
    ing.add_argument("--tool", default="", help="the MCP tool that produced the payload")
    ing.add_argument("--from", dest="source", type=Path, required=True, help="JSON file or -")

    sub.add_parser("bootstrap", help="show the runtime config (derived or from YAML)")
    tv = sub.add_parser("trust-validation", help="confirm a validation command (local, gated)")
    tv.add_argument("--scope", required=True, help="'library' or a consumer service name")
    tv.add_argument("--name", required=True)
    tv.add_argument("--env", action="append", default=[], help="KEY=VALUE (repeatable)")
    tv.add_argument("--approve", action="store_true")
    tv.add_argument("argv", nargs=argparse.REMAINDER, help="-- the command and its arguments")
    cons = sub.add_parser("consumers", help="README-first consumer discovery (read-only)")
    cons.add_argument("--add", action="append", default=[], help="extra repo name or URL")
    for name, help_text in (
        ("impact", "consumer impact matrix for a plan (read-only)"),
        ("branch-plan", "branch/PR strategy per repository (read-only)"),
        ("release-notes", "draft GitHub Release notes from the plan's content (read-only)"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--plan", type=Path, required=True)
        p.add_argument("--consumer", action="append", default=[], help="selected services")
        p.add_argument("--out", type=Path, help="write markdown (release-notes) here")
    pub = sub.add_parser("publish-release-notes", help="create/update the DRAFT GitHub Release")
    pub.add_argument("--plan", type=Path, required=True)
    pub.add_argument("--github-repository", required=True, help="owner/name of the library")
    pub.add_argument("--approve", action="store_true")

    # ADR-0018: operations (correlation IDs, engine-backed events, audit) and Slack outbox
    op = sub.add_parser("op", help="release operation: start, actor, event, report, list")
    op.add_argument("action", choices=["start", "actor", "event", "report", "list"])
    op.add_argument("--op", dest="op_id", help="operation id")
    op.add_argument("--kind", choices=["create-rc", "release", "reorder"])
    op.add_argument("--order", help="requested order (reorder)")
    op.add_argument("--ticket", action="append", default=[])
    op.add_argument("--repository", dest="op_repository", help="owner/name of the library")
    op.add_argument("--allow-concurrent", action="store_true")
    op.add_argument("--event")
    op.add_argument("--evidence", type=Path, help="engine JSON output that shows the event")
    op.add_argument("--step", help="APPROVAL_GRANTED: what the engineer approved")
    op.add_argument("--note", default="")
    op.add_argument("--from", dest="source", type=Path, help="identity payload (MCP result)")
    op.add_argument("--tool", help="MCP tool that produced --from")
    op.add_argument("--out", type=Path, help="report: write Markdown here")
    nt = sub.add_parser("notify", help="Slack (via MCP): channel, policy, plan, record, reconcile")
    nt.add_argument("action", choices=["channel", "policy", "plan", "record", "reconcile"])
    nt.add_argument("--op", dest="op_id")
    nt.add_argument("--event")
    nt.add_argument("--from", dest="source", type=Path, help="MCP result file")
    nt.add_argument("--name", default="library-release-orchestrator")
    nt.add_argument("--enable", action="store_true")
    nt.add_argument("--disable", action="store_true")
    nt.add_argument("--events", help="comma-separated events for the policy")
    nt.add_argument("--approve", action="store_true")

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

    if args.command == "mcp-ingest":
        return _mcp_ingest(args)
    if args.command == "trust-validation":
        config = _load_config(args)
        argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
        env = dict(item.split("=", 1) for item in args.env if "=" in item)
        return ExitCode.OK, trust_validation(
            config.state_dir,
            scope=args.scope,
            name=args.name,
            argv=argv,
            env=env,
            approve=args.approve,
        )

    if args.command == "notify" and args.action in ("channel", "policy"):
        return _notify_policy(args)

    if args.command == "context":
        context, _ = discover_context(
            args.path or Path.cwd(),
            config_path=_explicit_config(args),
            repository=args.repository_override,
            reader=GitHubReader(),
        )
        return (ExitCode.OK if context.ok else ExitCode.AMBIGUOUS_OR_NOT_FOUND), context.to_dict()

    config = _load_config(args)
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
            if args.command in MCP_COMMANDS:
                return _mcp_command(orch, args)
            return _discovery_command(orch, args)
    if args.command == "publish-release-notes":
        return _publish_notes(orch, args)
    if args.command in ("op", "notify"):
        return _operations(orch, args)

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
            bases=parse_bases(args.base),
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
    if args.action == "progress":
        if not args.preview:
            raise InvalidInputError("progress needs --preview FILE")
        result = engine.progress(json.loads(args.preview.read_text(encoding="utf-8")))
        return (ExitCode.BLOCKED if result["status"] == "DRIFT" else ExitCode.OK), result
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


def _load_config(args: argparse.Namespace) -> Config:
    """Explicit or discovered YAML (legacy, always wins), else the derived runtime config
    of the library repository the command runs in (ADR-0017)."""
    explicit = _explicit_config(args)
    if explicit is not None:
        return load_config(explicit)
    local = Path("release-orchestrator.yaml")
    if local.is_file():
        return load_config(local)
    context, config = discover_context(Path.cwd(), repository=args.repository_override)
    args.discovered_context = context
    if config is not None:
        return config
    if context.root is None or any(p.code == "CONFIG_MISMATCH" for p in context.problems):
        problems = [p.to_dict() for p in context.problems]
        raise ConfigError(
            "No release config describes this repository and it can't be derived here",
            details={"problems": problems, "searched_from": context.cwd},
        )
    boot = bootstrap(
        Path(context.root),
        own_repository=context.repository,
        extra_consumers=getattr(args, "add", None) or [],
    )
    args.bootstrap = boot
    _apply_bootstrap(context, boot)
    return boot.config


def _apply_bootstrap(context: RepoContext, boot: Bootstrap) -> None:
    context.config_path = str(boot.config.config_path)
    context.config_source = "derived from the repository (see runtime-config.json)"
    context.library = boot.config.library.name
    context.problems = [p for p in context.problems if p.code != "CONFIG_NOT_FOUND"]


def _context_for(orch: Orchestrator, args: argparse.Namespace) -> RepoContext:
    boot: Bootstrap | None = getattr(args, "bootstrap", None)
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
        config_path=None if boot is not None else orch.config.config_path,
        repository=args.repository_override,
        reader=orch.s.github,
    )
    if boot is not None:
        _apply_bootstrap(context, boot)
    return context


def _pull_source(orch: Orchestrator, context: RepoContext) -> PullSource | None:
    """Preferred: fresh GitHub MCP evidence; then a configured snapshot; then REST."""
    if context.repository:
        snap = McpStore(orch.config.state_dir / "mcp").get("github-pulls", context.repository)
        if snap is not None and snap.age_minutes() <= 30:
            return McpPullSource(snap)
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


# --------------------------------------------------------------------------- ADR-0017
def _mcp_ingest(args: argparse.Namespace) -> tuple[ExitCode, dict[str, Any]]:
    """Validate and store one MCP tool payload (local evidence store only)."""
    config = _load_config(args)
    raw = sys.stdin.read() if str(args.source) == "-" else args.source.read_text(encoding="utf-8")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = raw  # get_file_contents may hand over plain text
    snap = McpStore(config.state_dir / "mcp").ingest(
        args.kind, payload, repository=args.repository, key=args.key, tool=args.tool
    )
    summary = (
        snap.data
        if args.kind == "jira-issue"
        else {"records": (len(snap.data) if isinstance(snap.data, list) else 1)}
    )
    return ExitCode.OK, {
        "kind": snap.kind,
        "repository": snap.repository,
        "key": snap.key,
        "fetched_at": snap.fetched_at,
        "summary": summary,
    }


def _repositories(orch: Orchestrator, context: RepoContext) -> dict[str, str]:
    """Service -> owner/name from the consumers' origin remotes (for MCP evidence)."""
    out: dict[str, str] = dict(orch.config.github_repositories)
    if context.repository:
        out.setdefault(orch.config.library.name, context.repository)
    for consumer in orch.config.consumers:
        try:
            parsed = parse_remote(Git(consumer.repo).remote_url(orch.config.remote_name))
        except OrchestratorError:
            parsed = None
        if parsed:
            out.setdefault(consumer.name, parsed[1])
    return out


def _discovery(orch: Orchestrator, args: argparse.Namespace, context: RepoContext) -> Any:
    boot: Bootstrap | None = getattr(args, "bootstrap", None)
    if boot is not None and not getattr(args, "add", None):
        return boot.discovery
    top = orch.config.library.package_dir.strip("/").rsplit("/", 1)[-1]
    return discover_consumers(
        orch.config.library.repo,
        library=orch.config.library.name,
        top_package=top,
        own_repository=context.repository,
        workspace=[orch.config.library.repo.parent],
        store=McpStore(orch.config.state_dir / "mcp"),
        extra=getattr(args, "add", None) or [],
    )


def _mcp_command(orch: Orchestrator, args: argparse.Namespace) -> tuple[ExitCode, dict[str, Any]]:
    context = _context_for(orch, args)
    if args.command == "bootstrap":
        boot: Bootstrap | None = getattr(args, "bootstrap", None)
        if boot is None:
            return ExitCode.OK, {
                "source": f"YAML ({orch.config.config_path})",
                "note": "an explicit config is used as-is; nothing was derived",
                "context": context.to_dict(),
            }
        return ExitCode.OK, {"source": "derived", **boot.to_dict(), "context": context.to_dict()}
    if args.command == "consumers":
        result = _discovery(orch, args, context)
        return ExitCode.OK, result.to_dict()
    plan = load_plan(args.plan, orch.config)
    run = orch.status(plan)
    repos = _repositories(orch, context)
    if args.command == "impact":
        matrix = impact_matrix(
            plan,
            orch.s.git,
            repositories=repos,
            discovery=_discovery(orch, args, context),
            run_steps=run.steps,
        )
        return ExitCode.OK, matrix
    if args.command == "branch-plan":
        return ExitCode.OK, plan_branches(
            plan,
            orch.config,
            selected=args.consumer or None,
            store=McpStore(orch.config.state_dir / "mcp"),
            repositories=repos,
        )
    pulls = _pull_source(orch, context)
    notes = build_notes(
        plan,
        orch.s.git,
        tag_prefix=orch.config.library.tag_prefix,
        tracker=orch.s.tracker,
        pulls=pulls.list(state="all") if pulls is not None else [],
        impact=impact_matrix(
            plan, orch.s.git, repositories=repos, discovery=None, run_steps=run.steps
        ),
        run_steps=run.steps,
        key_pattern=orch.config.change_key_pattern,
    )
    markdown = render_notes(notes, library=orch.config.library.name)
    if args.out:
        args.out.write_text(markdown, encoding="utf-8")
    return ExitCode.OK, {
        "notes": notes,
        "markdown": markdown,
        "pull_request_source": pulls.source if pulls is not None else "unavailable",
        "status": "draft (not published)",
    }


def _publish_notes(orch: Orchestrator, args: argparse.Namespace) -> tuple[ExitCode, dict[str, Any]]:
    plan = load_plan(args.plan, orch.config)
    run = orch.status(plan)
    context = _context_for(orch, args)
    pulls = _pull_source(orch, context)
    notes = build_notes(
        plan,
        orch.s.git,
        tag_prefix=orch.config.library.tag_prefix,
        tracker=orch.s.tracker,
        pulls=pulls.list(state="all") if pulls is not None else [],
        impact=None,
        run_steps=run.steps,
        key_pattern=orch.config.change_key_pattern,
    )
    result = publish_notes(
        plan,
        orch.s.git,
        notes_markdown=render_notes(notes, library=orch.config.library.name),
        repository=validate_repository(args.github_repository),
        tag=notes["tag"],
        run_steps=run.steps,
        remote=orch.config.remote_name,
        approve=args.approve,
        writer=GitHubReleaseWriter(api_url=orch.config.github_api_url),
    )
    return ExitCode.OK, result


# --------------------------------------------------------------------------- operations (ADR-0018)
def _read_source(args: argparse.Namespace) -> str:
    if args.source is None:
        raise InvalidInputError("--from FILE (the MCP tool result) is required")
    return str(args.source.read_text(encoding="utf-8"))


def _notify_policy(args: argparse.Namespace) -> tuple[ExitCode, dict[str, Any]]:
    if args.action == "channel":
        return ExitCode.OK, resolve_channel(_read_source(args), args.name)
    if not (args.enable or args.disable):
        return ExitCode.OK, load_policy()
    channel = resolve_channel(_read_source(args), args.name) if args.enable else None
    return ExitCode.OK, set_policy(
        channel=channel,
        enable=args.enable,
        events=_split(args.events),
        approve=args.approve,
    )


def _engine_identity() -> dict[str, Any]:
    from release_orchestrator import __version__

    return {
        "version": __version__,
        "revision": os.environ.get("LRO_ENGINE_REVISION", "development checkout"),
    }


def _operations(orch: Orchestrator, args: argparse.Namespace) -> tuple[ExitCode, dict[str, Any]]:
    store = OperationStore(orch.config.state_dir)
    if args.command == "notify":
        if not (args.op_id and (args.event or args.action == "reconcile")):
            raise InvalidInputError("notify plan/record need --op and --event; reconcile --op")
        notifier = Notifier(store)
        if args.action == "plan":
            return ExitCode.OK, notifier.plan(args.op_id, args.event)
        if args.action == "record":
            return ExitCode.OK, notifier.record(args.op_id, args.event, _read_source(args))
        return ExitCode.OK, notifier.reconcile(args.op_id, _read_source(args))
    if args.action == "list":
        return ExitCode.OK, {"active": store.active(orch.config.library.name)}
    if args.action == "start":
        if not args.kind:
            raise InvalidInputError("op start needs --kind")
        request = {"order": _split(args.order) or [], "tickets": args.ticket}
        return ExitCode.OK, store.start(
            kind=args.kind,
            library=orch.config.library.name,
            repository=args.op_repository,
            request=request,
            engine=_engine_identity(),
            allow_concurrent=args.allow_concurrent,
        )
    if not args.op_id:
        raise InvalidInputError(f"op {args.action} needs --op")
    if args.action == "actor":
        return ExitCode.OK, store.set_actor(
            args.op_id, _read_source(args), args.tool or "unspecified MCP tool"
        )
    if args.action == "event":
        if not args.event:
            raise InvalidInputError("op event needs --event")
        return ExitCode.OK, store.record(
            args.op_id,
            args.event,
            evidence=args.evidence,
            note=args.note,
            approved_step=args.step,
        )
    report = store.report(args.op_id)
    if args.out:
        args.out.write_text(render_report(report), encoding="utf-8")
    return ExitCode.OK, report
