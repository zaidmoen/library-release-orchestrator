"""Dependency analysis: can the requested changes ship without the excluded ones?

Evidence is layered and every layer is reported separately:

1. Static Python analysis: symbols, modules and import bindings that the requested
   change *uses* on its changed lines but that only an excluded change *introduces*
   -> ``DIRECT_CODE_DEPENDENCY`` / ``MISSING_PREREQUISITE`` (blockers).
2. Behavioural coupling: the requested change uses a symbol an excluded change
   *modified* -> ``SHARED_SYMBOL_MODIFIED`` (warning; tests must decide).
3. Textual overlap: shared files (warning) and a real ``cherry-pick`` probe in a
   throwaway clone -> ``GIT_CONFLICT`` (blocker).
4. Coverage gaps: non-Python files and unparsable sources are reported as unverified.

A clean probe is *never* treated as proof of independence.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from release_orchestrator import cache as disk_cache
from release_orchestrator.analysis import python_symbols as pysym
from release_orchestrator.analysis.graph import ChangeGraph
from release_orchestrator.fsutil import temp_dir
from release_orchestrator.gitutil.history import HistoryScan
from release_orchestrator.gitutil.repo import Git, clone_no_checkout, optimizations_enabled
from release_orchestrator.models import (
    ChangeUnit,
    CommitInfo,
    DependencyFinding,
    FindingKind,
    Severity,
    Verdict,
)

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_DOC_SUFFIXES = (".md", ".rst", ".txt")


@dataclass
class ChangeEffects:
    """What a change introduces, modifies and removes (from its own diffs)."""

    key: str
    introduced: set[pysym.Symbol] = field(default_factory=set)
    modified: set[pysym.Symbol] = field(default_factory=set)
    removed_public: set[pysym.Symbol] = field(default_factory=set)
    new_modules: set[str] = field(default_factory=set)
    new_bindings: set[tuple[str, str]] = field(default_factory=set)  # (path, name)
    references: list[tuple[str, pysym.Reference]] = field(default_factory=list)  # (path, ref)
    parse_errors: list[str] = field(default_factory=list)
    unverified_files: list[str] = field(default_factory=list)


@dataclass
class ProbeResult:
    applied_cleanly: bool
    applied: list[str]
    conflict_commit: str | None = None
    conflicted_files: list[str] = field(default_factory=list)
    message: str = ""
    result_patch_ids: dict[str, str] = field(default_factory=dict)  # source sha -> patch-id

    def to_dict(self) -> dict[str, Any]:
        return {
            "applied_cleanly": self.applied_cleanly,
            "applied_commits": self.applied,
            "conflict_commit": self.conflict_commit,
            "conflicted_files": self.conflicted_files,
            "message": self.message,
            "result_patch_ids": self.result_patch_ids,
            "note": "A clean apply shows textual applicability only, not independence.",
        }


@dataclass
class AnalysisResult:
    requested: list[str]
    excluded: list[str]
    already_released: list[str]
    verdict: Verdict
    findings: list[DependencyFinding]
    graph: ChangeGraph
    prerequisites: list[str]
    cycles: list[list[str]]
    probe: ProbeResult | None
    api_removals: list[str]

    @property
    def blockers(self) -> list[DependencyFinding]:
        return [f for f in self.findings if f.severity is Severity.BLOCKER]

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": str(self.verdict),
            "requested_changes": self.requested,
            "excluded_changes": self.excluded,
            "already_released": self.already_released,
            "prerequisites": self.prerequisites,
            "cycles": self.cycles,
            "findings": [f.to_dict() for f in self.findings],
            "dependency_graph": self.graph.to_dict(),
            "probe": self.probe.to_dict() if self.probe else None,
            "public_api_removals": self.api_removals,
            "confidence_note": (
                "Static analysis covers Python symbol/module usage only. Runtime, data and "
                "configuration dependencies are not detectable statically; validation tests "
                "are required before release."
            ),
        }


def all_units(scan: HistoryScan) -> dict[str, ChangeUnit]:
    """Real changes plus pseudo-changes for unmapped/ambiguous commits.

    Unmapped commits still sit between base and source, so a requested change may depend
    on them; modelling them as excluded units keeps the analysis honest.
    """
    units = dict(scan.changes)
    for commit in scan.unmapped:
        key = f"UNMAPPED:{commit.sha[:10]}"
        units[key] = ChangeUnit(key, (commit,), commit.subject, provenance="unmapped")
    for commit in scan.ambiguous_commits:
        key = f"AMBIGUOUS:{commit.sha[:10]}"
        units[key] = ChangeUnit(key, (commit,), commit.subject, provenance="ambiguous")
    return units


class DependencyAnalyzer:
    def __init__(self, git: Git, package_dir: str) -> None:
        self.git = git
        self.package_dir = package_dir.strip("/")
        self._cache: dict[str, ChangeEffects] = {}

    # ------------------------------------------------------------------ facts
    def effects(self, unit: ChangeUnit) -> ChangeEffects:
        if unit.key in self._cache:
            return self._cache[unit.key]
        eff = ChangeEffects(unit.key)
        if optimizations_enabled():
            # ADR-0016: effects are a pure function of each commit's content, so they are
            # computed once per commit (process memo + immutable disk cache) and merged in
            # commit order, which is exactly what sequential _collect produces.
            for commit in unit.commits:
                _merge(eff, self._commit_effects(commit))
        else:
            for commit in unit.commits:
                self._collect(commit, eff)
        self._cache[unit.key] = eff
        return eff

    def _commit_effects(self, commit: CommitInfo) -> ChangeEffects:
        key = (str(self.git.path.resolve()), commit.sha, self.package_dir)
        hit = _EFFECTS_MEMO.get(key)
        if hit is not None:
            return hit
        stored = disk_cache.active().get("effects", commit.sha, self.package_dir)
        eff = _effects_from_json(stored) if stored is not None else None
        if eff is None:
            eff = ChangeEffects("")
            self._collect(commit, eff)
            disk_cache.active().put("effects", _effects_to_json(eff), commit.sha, self.package_dir)
        _EFFECTS_MEMO[key] = eff
        return eff

    def _collect(self, commit: CommitInfo, eff: ChangeEffects) -> None:
        parent = commit.parents[0] if commit.parents else None
        added_lines = _added_lines(self.git.unified_diff(commit.sha)) if commit.files else {}
        for fc in commit.files:
            if not fc.path.endswith(".py"):
                if fc.path not in eff.unverified_files:
                    eff.unverified_files.append(fc.path)
                continue
            module = pysym.module_name(fc.path, self.package_dir) or fc.path
            before_src = (
                self.git.show_file(parent, fc.old_path or fc.path)
                if parent and fc.status != "A"
                else None
            )
            after_src = self.git.show_file(commit.sha, fc.path) if fc.status != "D" else None
            try:
                before = pysym.parse(before_src) if before_src is not None else None
                after = pysym.parse(after_src) if after_src is not None else None
            except SyntaxError as exc:
                eff.parse_errors.append(f"{fc.path}@{commit.short}: {exc.msg} (line {exc.lineno})")
                continue
            is_pkg = fc.path.endswith("__init__.py")
            empty = pysym.ModuleFacts(module)
            bf = pysym.module_facts(before, module, is_package=is_pkg) if before else empty
            af = pysym.module_facts(after, module, is_package=is_pkg) if after else empty
            if before is None and after is not None:
                eff.new_modules.add(module)
            in_package = fc.path.startswith(self.package_dir + "/")
            for name, dump in af.definitions.items():
                if name not in bf.definitions:
                    eff.introduced.add((module, name))
                elif bf.definitions[name] != dump:
                    eff.modified.add((module, name))
            if in_package:
                for name in pysym.public_names(bf) - pysym.public_names(af):
                    eff.removed_public.add((module, name))
            for name in af.bindings.keys() - bf.bindings.keys():
                eff.new_bindings.add((fc.path, name))
            if after is not None:
                lines = added_lines.get(fc.path, set())
                for ref in pysym.references_on_lines(after, af, lines):
                    eff.references.append((fc.path, ref))

    # ------------------------------------------------------------------ analysis
    def pairwise(self, requested: ChangeUnit, excluded: ChangeUnit) -> list[DependencyFinding]:
        r_eff, e_eff = self.effects(requested), self.effects(excluded)
        findings: dict[tuple[str, str], DependencyFinding] = {}

        for path, ref in r_eff.references:
            if ref.target is None:
                continue
            module, attr = ref.target
            symbol = f"{module}.{attr}" if attr else module
            where = {"file": path, "line": ref.line, "symbol": symbol}
            kind: FindingKind | None = None
            needs_new_module = (
                module in e_eff.new_modules or symbol in e_eff.new_modules
            ) and not (module in r_eff.new_modules or symbol in r_eff.new_modules)
            needs_new_symbol = (module, attr) in e_eff.introduced and (
                module,
                attr,
            ) not in r_eff.introduced
            needs_new_binding = (path, ref.name) in e_eff.new_bindings and (
                path,
                ref.name,
            ) not in r_eff.new_bindings
            if needs_new_module:
                kind = FindingKind.MISSING_PREREQUISITE
            elif needs_new_symbol or needs_new_binding:
                kind = FindingKind.DIRECT_CODE_DEPENDENCY
            elif (module, attr) in e_eff.modified:
                kind = FindingKind.SHARED_SYMBOL_MODIFIED
            if kind is None or (kind, symbol) in findings:
                continue
            blocking = kind is not FindingKind.SHARED_SYMBOL_MODIFIED
            findings[(kind, symbol)] = DependencyFinding(
                kind=kind,
                severity=Severity.BLOCKER if blocking else Severity.WARNING,
                change=requested.key,
                depends_on=excluded.key,
                message=_explain(kind, requested.key, excluded.key, symbol),
                evidence=where,
            )

        shared = sorted(set(requested.files) & set(excluded.files))
        result = list(findings.values())
        if shared:
            result.append(
                DependencyFinding(
                    kind=FindingKind.SHARED_FILE,
                    severity=Severity.WARNING,
                    change=requested.key,
                    depends_on=excluded.key,
                    message=f"{requested.key} and {excluded.key} modify the same file(s)",
                    evidence={"files": shared},
                )
            )
        return result

    def coverage_findings(self, unit: ChangeUnit) -> list[DependencyFinding]:
        eff = self.effects(unit)
        out = []
        code_like = [f for f in eff.unverified_files if not f.endswith(_DOC_SUFFIXES)]
        if code_like:
            out.append(
                DependencyFinding(
                    kind=FindingKind.UNVERIFIED_FILE,
                    severity=Severity.WARNING,
                    change=unit.key,
                    message="Non-Python files are not covered by static dependency analysis",
                    evidence={"files": code_like},
                )
            )
        if eff.parse_errors:
            out.append(
                DependencyFinding(
                    kind=FindingKind.PARSE_ERROR,
                    severity=Severity.WARNING,
                    change=unit.key,
                    message="Some Python sources could not be parsed; dependencies are unverified",
                    evidence={"errors": eff.parse_errors},
                )
            )
        return out

    def analyze(
        self,
        scan: HistoryScan,
        requested: list[str],
        *,
        probe: bool = True,
    ) -> AnalysisResult:
        units = all_units(scan)
        findings: list[DependencyFinding] = []
        graph = ChangeGraph(units)

        for sha, keys in scan.ambiguous.items():
            for key in keys:
                if key in requested:
                    findings.append(
                        DependencyFinding(
                            kind=FindingKind.AMBIGUOUS_COMMIT,
                            severity=Severity.BLOCKER,
                            change=key,
                            depends_on=f"AMBIGUOUS:{sha[:10]}",
                            message=f"Commit {sha[:10]} references several changes {keys}; "
                            "its ownership must be clarified before release",
                            evidence={"commit": sha, "keys": keys},
                        )
                    )
                    graph.add_edge(key, f"AMBIGUOUS:{sha[:10]}")
        for sha in scan.merges_skipped:
            findings.append(
                DependencyFinding(
                    kind=FindingKind.MERGE_COMMIT_SKIPPED,
                    severity=Severity.WARNING,
                    change="*",
                    message=f"Merge commit {sha[:10]} is not replayed; only non-merge commits are",
                    evidence={"commit": sha},
                )
            )

        # Iterate to a fixpoint so that transitive prerequisites are found.
        selected = list(requested)
        first_round = True
        while True:
            new_deps: set[str] = set()
            for r_key in selected:
                for e_key, e_unit in units.items():
                    if e_key in selected:
                        continue
                    for finding in self.pairwise(units[r_key], e_unit):
                        if first_round:
                            findings.append(finding)
                        if finding.severity is Severity.BLOCKER:
                            graph.add_edge(r_key, e_key)
                            new_deps.add(e_key)
            first_round = False
            if not new_deps - set(selected):
                break
            selected.extend(sorted(new_deps - set(selected)))

        for key in requested:
            findings.extend(self.coverage_findings(units[key]))

        api_removals = sorted(
            f"{m}.{n}" for key in requested for (m, n) in self.effects(units[key]).removed_public
        )
        for symbol in api_removals:
            findings.append(
                DependencyFinding(
                    kind=FindingKind.PUBLIC_API_REMOVED,
                    severity=Severity.WARNING,
                    change="*",
                    message=f"Public symbol {symbol} is removed: breaking change for consumers",
                    evidence={"symbol": symbol},
                )
            )

        probe_result = None
        if probe:
            ordered = [
                sha for sha in scan.commit_order() if any(sha in units[k].shas for k in requested)
            ]
            probe_result = cached_probe(self.git.path, scan.base_sha, ordered)
            if not probe_result.applied_cleanly:
                causes = sorted(
                    key
                    for key, unit in units.items()
                    if key not in requested and set(unit.files) & set(probe_result.conflicted_files)
                )
                owner = next(
                    (k for k in requested if probe_result.conflict_commit in units[k].shas),
                    requested[0],
                )
                findings.append(
                    DependencyFinding(
                        kind=FindingKind.GIT_CONFLICT,
                        severity=Severity.BLOCKER,
                        change=owner,
                        depends_on=causes[0] if len(causes) == 1 else None,
                        message=(
                            f"Commit {str(probe_result.conflict_commit)[:10]} of {owner} does "
                            "not apply cleanly on the base"
                        ),
                        evidence={
                            "conflicted_files": probe_result.conflicted_files,
                            "suspected_causes": causes,
                        },
                    )
                )

        prerequisites = sorted(graph.closure(requested) - set(requested))
        return AnalysisResult(
            requested=list(requested),
            excluded=sorted(k for k in units if k not in requested),
            already_released=sorted(scan.already_released),
            verdict=_verdict(findings),
            findings=findings,
            graph=graph,
            prerequisites=prerequisites,
            cycles=graph.cycles(),
            probe=probe_result,
            api_removals=api_removals,
        )


def probe_apply(repo: Path, base_sha: str, shas: Iterable[str]) -> ProbeResult:
    """Cherry-pick ``shas`` onto ``base_sha`` inside a throwaway clone (non-mutating)."""
    applied: list[str] = []
    patch_ids: dict[str, str] = {}
    with temp_dir("lro-probe-") as tmp:
        sandbox = clone_no_checkout(repo, tmp / "sandbox")
        sandbox.run("checkout", "--quiet", "--detach", base_sha)
        for sha in shas:
            ok, conflicts, message = sandbox.cherry_pick(sha)
            if not ok:
                return ProbeResult(False, applied, sha, conflicts, message, patch_ids)
            applied.append(sha)
            patch_ids[sha] = sandbox.patch_id("HEAD")
    return ProbeResult(True, applied, result_patch_ids=patch_ids)


_EFFECTS_MEMO: dict[tuple[str, str, str], ChangeEffects] = {}
_PROBE_MEMO: dict[tuple[str, ...], ProbeResult] = {}


def _merge(into: ChangeEffects, part: ChangeEffects) -> None:
    into.introduced |= part.introduced
    into.modified |= part.modified
    into.removed_public |= part.removed_public
    into.new_modules |= part.new_modules
    into.new_bindings |= part.new_bindings
    into.references.extend(part.references)
    into.parse_errors.extend(part.parse_errors)
    for path in part.unverified_files:
        if path not in into.unverified_files:
            into.unverified_files.append(path)


def _effects_to_json(e: ChangeEffects) -> dict[str, Any]:
    return {
        "introduced": sorted([m, n] for m, n in e.introduced),
        "modified": sorted([m, n] for m, n in e.modified),
        "removed_public": sorted([m, n] for m, n in e.removed_public),
        "new_modules": sorted(e.new_modules),
        "new_bindings": sorted([p, n] for p, n in e.new_bindings),
        "references": [
            [p, r.name, list(r.target) if r.target is not None else None, r.line]
            for p, r in e.references
        ],
        "parse_errors": list(e.parse_errors),
        "unverified_files": list(e.unverified_files),
    }


def _effects_from_json(d: Any) -> ChangeEffects | None:
    try:
        return ChangeEffects(
            "",
            introduced={(str(m), str(n)) for m, n in d["introduced"]},
            modified={(str(m), str(n)) for m, n in d["modified"]},
            removed_public={(str(m), str(n)) for m, n in d["removed_public"]},
            new_modules={str(m) for m in d["new_modules"]},
            new_bindings={(str(p), str(n)) for p, n in d["new_bindings"]},
            references=[
                (str(p), pysym.Reference(str(name), tuple(t) if t is not None else None, int(ln)))
                for p, name, t, ln in d["references"]
            ],
            parse_errors=[str(x) for x in d["parse_errors"]],
            unverified_files=[str(x) for x in d["unverified_files"]],
        )
    except (KeyError, TypeError, ValueError):
        return None  # corrupted entry: recompute


def cached_probe(repo: Path, base_sha: str, shas: list[str]) -> ProbeResult:
    """``probe_apply`` is a pure function of (base, ordered commits): memo + disk cache."""
    if not optimizations_enabled():
        return probe_apply(repo, base_sha, shas)
    key = (base_sha, *shas)
    hit = _PROBE_MEMO.get(key)
    if hit is not None:
        return hit
    stored = disk_cache.active().get("probe", *key)
    result = None
    if isinstance(stored, dict):
        try:
            result = ProbeResult(
                bool(stored["applied_cleanly"]),
                [str(s) for s in stored["applied"]],
                stored["conflict_commit"],
                [str(f) for f in stored["conflicted_files"]],
                str(stored["message"]),
                {str(k): str(v) for k, v in stored["result_patch_ids"].items()},
            )
        except (KeyError, TypeError, AttributeError):
            result = None
    if result is None:
        result = probe_apply(repo, base_sha, shas)
        disk_cache.active().put(
            "probe",
            {
                "applied_cleanly": result.applied_cleanly,
                "applied": result.applied,
                "conflict_commit": result.conflict_commit,
                "conflicted_files": result.conflicted_files,
                "message": result.message,
                "result_patch_ids": result.result_patch_ids,
            },
            *key,
        )
    _PROBE_MEMO[key] = result
    return result


def clear_analysis_memo() -> None:
    _EFFECTS_MEMO.clear()
    _PROBE_MEMO.clear()


def _added_lines(diff: str) -> dict[str, set[int]]:
    result: dict[str, set[int]] = {}
    current: str | None = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            target = line[4:]
            current = target[2:] if target.startswith("b/") else None
        elif current and (m := _HUNK_RE.match(line)):
            start, count = int(m.group(1)), int(m.group(2) or "1")
            result.setdefault(current, set()).update(range(start, start + count))
    return result


def _explain(kind: FindingKind, change: str, other: str, symbol: str) -> str:
    if kind is FindingKind.MISSING_PREREQUISITE:
        return f"{change} imports '{symbol}', a module that only exists after {other}"
    if kind is FindingKind.DIRECT_CODE_DEPENDENCY:
        return f"{change} uses '{symbol}', which is introduced by {other}"
    return (
        f"{change} uses '{symbol}', whose behaviour {other} modified; results may differ without it"
    )


def _verdict(findings: list[DependencyFinding]) -> Verdict:
    kinds = {f.kind for f in findings if f.severity is Severity.BLOCKER}
    if kinds & {FindingKind.DIRECT_CODE_DEPENDENCY, FindingKind.MISSING_PREREQUISITE}:
        return Verdict.DEPENDENT
    if FindingKind.GIT_CONFLICT in kinds:
        return Verdict.CONFLICT
    if kinds:
        return Verdict.UNKNOWN
    if any(f.severity is Severity.WARNING for f in findings):
        return Verdict.INDEPENDENT_WITH_RISKS
    return Verdict.INDEPENDENT
