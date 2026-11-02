"""Caller discovery: which microservices consume the library, and which need this release."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Any

from release_orchestrator.config import ConsumerConfig
from release_orchestrator.gitutil.repo import Git
from release_orchestrator.versioning.semver import SemVer

_REQ_RE = re.compile(
    r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*([=<>!~]=?)?\s*([^\s;#,]*)"
)


def normalize(name: str) -> str:
    """PEP 503 normalisation."""
    return re.sub(r"[-_.]+", "-", name).lower()


@dataclass(frozen=True)
class Requirement:
    line_no: int
    operator: str | None
    version: str


def find_requirement(text: str, library: str) -> Requirement | None:
    for i, line in enumerate(text.splitlines()):
        match = _REQ_RE.match(line)
        if match and normalize(match.group(1)) == normalize(library):
            return Requirement(i, match.group(2), match.group(3))
    return None


def library_imports(git: Git, rev: str, top_package: str) -> set[str]:
    """Modules of the library imported by any Python file at ``rev`` (static, no execution)."""
    listing = git.out("ls-tree", "-r", "--name-only", rev)
    modules: set[str] = set()
    for path in listing.splitlines():
        if not path.endswith(".py"):
            continue
        source = git.show_file(rev, path)
        if source is None:
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            modules.add(top_package)  # unknown usage: assume the whole package
            continue
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
            modules.update(n for n in names if n == top_package or n.startswith(top_package + "."))
    return modules


@dataclass
class ConsumerTarget:
    name: str
    current_version: str | None
    operator: str | None
    proposed_version: str
    action: str  # update | up_to_date | manual | not_targeted | not_a_consumer
    reasons: list[str] = field(default_factory=list)
    imported_modules: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "current_version": self.current_version,
            "operator": self.operator,
            "proposed_version": self.proposed_version,
            "action": self.action,
            "reasons": self.reasons,
            "imported_library_modules": self.imported_modules,
            "rollback_version": self.current_version if self.action == "update" else None,
        }


def assess_consumer(
    consumer: ConsumerConfig,
    *,
    library: str,
    top_package: str,
    affected_modules: set[str],
    requested_by_ticket: set[str],
    proposed: SemVer,
) -> ConsumerTarget:
    git = Git(consumer.repo)
    rev = git.rev_parse(consumer.source_ref)
    text = git.show_file(rev, consumer.dependency_file)
    requirement = find_requirement(text, library) if text is not None else None
    target = ConsumerTarget(consumer.name, None, None, str(proposed), "not_a_consumer")
    if requirement is None:
        target.reasons.append(f"{consumer.dependency_file} does not declare {library}")
        return target

    target.current_version = requirement.version or None
    target.operator = requirement.operator
    imported = library_imports(git, rev, top_package)
    target.imported_modules = sorted(imported)
    impacted = top_package in imported or any(
        m == a or m.startswith(a + ".") or a.startswith(m + ".")
        for m in imported
        for a in affected_modules
    )
    if requested_by_ticket:
        targeted = consumer.name in requested_by_ticket
        target.reasons.append("requested by ticket" if targeted else "not requested by ticket(s)")
        if targeted and not impacted:
            target.reasons.append("note: no import of the changed modules was found")
        if not targeted and impacted:
            target.reasons.append("imports changed modules; may upgrade later")
    else:
        targeted = impacted
        target.reasons.append(
            "imports changed modules" if impacted else "does not import changed modules"
        )
    if not targeted:
        target.action = "not_targeted"
        return target

    if requirement.operator != "==":
        target.action = "manual"
        target.reasons.append("dependency is not pinned with '=='; update manually")
        return target
    current = SemVer.try_parse(requirement.version)
    if current is not None and current >= proposed:
        target.action = "up_to_date"
        return target
    target.action = "update"
    return target
