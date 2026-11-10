"""``release-orchestrator`` command-line interface.

stdout carries exactly one result: a JSON envelope (``--json``) or a concise text summary.
Logs go to stderr. Exit codes are stable (see ``errors.ExitCode`` and docs/cli.md).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from release_orchestrator import __version__
from release_orchestrator.config import load_config
from release_orchestrator.demo import SCENARIOS, build_demo
from release_orchestrator.environments import EnvironmentLedger
from release_orchestrator.errors import BlockedError, ExitCode, OrchestratorError
from release_orchestrator.lab import build_lab
from release_orchestrator.planning.manifest import load_plan
from release_orchestrator.workflow import Orchestrator, RequestInput, Services, write_plan

WRITE_COMMANDS = {"prepare", "pr", "publish", "update-consumers", "push", "promote", "rollback"}


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {"level": record.levelname, "logger": record.name, "message": record.getMessage()}
        if hasattr(record, "argv"):
            data["argv"] = record.argv
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
        default=Path(os.environ.get("LRO_CONFIG", "release-orchestrator.yaml")),
        help="trusted config file (default: $LRO_CONFIG or ./release-orchestrator.yaml)",
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

    if args.command == "lab-init":
        return ExitCode.OK, {"config": str(build_lab(args.dest))}

    config = load_config(args.config)
    orch = Orchestrator(Services.from_config(config))

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
        return ExitCode.OK, orch.update_consumers(plan, approve=args.approve, dry_run=args.dry_run)
    if args.command == "push":
        return ExitCode.OK, orch.push(plan, approve=args.approve)
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
    error: dict[str, Any] | None = None
    result: dict[str, Any] = {}
    try:
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
