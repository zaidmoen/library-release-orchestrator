"""Configuration-free runtime config for ``/library-release`` (ADR-0017).

When no ``release-orchestrator.yaml`` describes the repository, the engine derives a
runtime configuration from **trusted local facts** and validates it with the very same
``parse_config`` used for YAML:

==================  ===============================================================
library name        ``pyproject.toml`` ``[project].name`` / Poetry / ``setup.cfg``
package directory   ``src/<pkg>/__init__.py`` or ``<pkg>/__init__.py``
version file        ``__version__`` in the package, else a static pyproject version
tag prefix          existing release tags (``v1.2.3`` → ``v``)
source branch       the repository's default branch (context discovery)
consumers           README-first discovery, **confirmed** local repositories only
registry            ``local`` under the state directory (never production)
state directory     ``<git-common-dir>/lro-state`` (never committed)
==================  ===============================================================

Never inferred from repository text: validation commands, production publishing,
environments or approval policies. Validation commands come only from a trusted source:
a YAML config, or commands the engineer confirmed with ``trust-validation --approve``
(stored in ``<state>/trusted-validation.json``, outside the working tree). The engine
may *suggest* a command (shown, never adopted); without one, ``NO_VALIDATION_CONFIGURED``
keeps every plan blocked. Every value records its source, and the resolved
config is written to ``<state>/runtime-config.json`` for audit. An explicit YAML always
wins and is never modified.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from configparser import ConfigParser
from configparser import Error as ConfigParserError
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from release_orchestrator.config import Config, parse_config
from release_orchestrator.consumers.discovery import normalize
from release_orchestrator.consumers.readme_discovery import (
    DiscoveryResult,
    discover_consumers,
)
from release_orchestrator.errors import ConfigError, GitError
from release_orchestrator.gitutil.repo import Git
from release_orchestrator.integrations.mcp_snapshot import McpStore
from release_orchestrator.versioning.semver import SemVer

_VERSION_ASSIGN = re.compile(r'__version__\s*=\s*"([^"]+)"')
TRUSTED_VALIDATION = "trusted-validation.json"


def load_trusted_validation(state: Path) -> dict[str, list[dict[str, Any]]]:
    """``{"library": [...], "<service>": [...]}`` confirmed by the engineer."""
    path = state / TRUSTED_VALIDATION
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"{path} is unreadable: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must map scopes to command lists")
    return {str(k): list(v) for k, v in data.items() if isinstance(v, list)}


def trust_validation(
    state: Path, *, scope: str, name: str, argv: list[str], env: dict[str, str], approve: bool
) -> dict[str, Any]:
    """Record an engineer-confirmed validation command (validated like YAML commands)."""
    from release_orchestrator.config import _commands  # same validation as the YAML path
    from release_orchestrator.errors import ApprovalRequiredError

    if not approve:
        raise ApprovalRequiredError(
            "Trusting a validation command lets the engine run it: it needs an explicit yes",
            details={"scope": scope, "argv": argv},
        )
    entry = {"name": name, "argv": argv, "env": env}
    _commands([entry], f"trusted validation ({scope})")
    data = load_trusted_validation(state)
    data[scope] = [e for e in data.get(scope, []) if e.get("name") != name] + [entry]
    state.mkdir(parents=True, exist_ok=True)
    tmp = state / (TRUSTED_VALIDATION + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, state / TRUSTED_VALIDATION)
    return {"scope": scope, "trusted": data[scope]}


def suggest_validation(root: Path, package_dir: str) -> dict[str, Any]:
    """A *suggestion* only, shown to the engineer; never adopted without confirmation."""
    text = (
        (root / "pyproject.toml").read_text(encoding="utf-8")
        if (root / "pyproject.toml").is_file()
        else ""
    )
    if "pytest" in text:
        argv = ["{python}", "-m", "pytest", "-q"]
    else:
        argv = ["{python}", "-m", "unittest", "discover", "-s", "tests", "-t", "."]
    env = {"PYTHONPATH": "{library_path}/src"} if package_dir.startswith("src/") else {}
    return {"name": "unit-tests", "argv": argv, "env": env}


@dataclass
class Bootstrap:
    config: Config
    raw: dict[str, Any]
    provenance: dict[str, str]
    discovery: DiscoveryResult
    questions: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "config": self.raw,
            "provenance": self.provenance,
            "consumer_discovery": self.discovery.to_dict(),
            "questions": self.questions,
            "not_inferred": [
                "validation commands (READY needs a trusted config that defines them)",
                "production publishing (the registry is local)",
                "environments and approval policies",
            ],
        }


def state_dir_for(root: Path) -> Path:
    git = Git(root)
    common = Path(git.out("rev-parse", "--git-common-dir"))
    common = common if common.is_absolute() else (root / common)
    return common.resolve() / "lro-state"


def _package_metadata(root: Path) -> tuple[str | None, str | None, str]:
    """(distribution name, static version, source)."""
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"pyproject.toml is not valid TOML: {exc}") from exc
        project = data.get("project") or {}
        if project.get("name"):
            return str(project["name"]), project.get("version"), "pyproject.toml [project]"
        poetry = (data.get("tool") or {}).get("poetry") or {}
        if poetry.get("name"):
            return str(poetry["name"]), poetry.get("version"), "pyproject.toml [tool.poetry]"
    setup_cfg = root / "setup.cfg"
    if setup_cfg.is_file():
        parser = ConfigParser()
        try:
            parser.read_string(setup_cfg.read_text(encoding="utf-8"))
        except ConfigParserError:
            parser = ConfigParser()
        name = parser.get("metadata", "name", fallback="")
        if name:
            return name, parser.get("metadata", "version", fallback=None), "setup.cfg [metadata]"
    return None, None, "not found"


def _package_dir(root: Path, dist: str) -> str | None:
    pkg = normalize(dist).replace("-", "_")
    for rel in (f"src/{pkg}", pkg):
        if (root / rel / "__init__.py").is_file():
            return rel
    found = sorted(p.parent for p in (root / "src").glob("*/__init__.py"))
    if len(found) == 1:
        return found[0].relative_to(root).as_posix()
    return None


def _tag_prefix(git: Git) -> tuple[str, str]:
    tags = git.tags("*")
    with_v = [t for t in tags if t.startswith("v") and SemVer.try_parse(t[1:])]
    bare = [t for t in tags if SemVer.try_parse(t)]
    if with_v and not bare:
        return "v", f"existing tags ({len(with_v)} like {with_v[0]})"
    if bare and not with_v:
        return "", f"existing tags ({len(bare)} like {bare[0]})"
    if with_v and bare:
        return "v", "mixed tag styles; 'v' assumed (confirm with a config to override)"
    return "v", "no release tags yet; default 'v'"


def _default_branch(git: Git) -> tuple[str, str]:
    proc = git.run("symbolic-ref", "--short", "-q", "refs/remotes/origin/HEAD", check=False)
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip().split("/", 1)[-1], "git (refs/remotes/origin/HEAD)"
    for name in ("main", "master"):
        if git.try_rev_parse(f"refs/heads/{name}"):
            return name, f"local branch '{name}' (remote default not known)"
    current = git.run("symbolic-ref", "--short", "-q", "HEAD", check=False).stdout.strip()
    if current:
        return current, "current branch (remote default not known)"
    raise ConfigError("Can't determine the source branch: detached HEAD and no main/master")


def bootstrap(
    root: Path,
    *,
    own_repository: str | None = None,
    extra_consumers: list[str] | None = None,
    workspace: list[Path] | None = None,
) -> Bootstrap:
    """Derive and validate the runtime config for the library repository at ``root``."""
    root = root.resolve()
    git = Git(root)
    if not git.is_repo():
        raise ConfigError(f"{root} is not a Git repository")
    prov: dict[str, str] = {}
    questions: list[dict[str, Any]] = []
    dist, static_version, meta_source = _package_metadata(root)
    if not dist:
        raise ConfigError(
            "No package metadata found (pyproject.toml / setup.cfg); can't identify the "
            "library. Pass --config with library.name and library.package_dir.",
            details={"needs": "--config <file>"},
        )
    prov["library.name"] = meta_source
    package_dir = _package_dir(root, dist)
    if package_dir is None:
        raise ConfigError(
            f"Can't find the import package for {dist} (looked for src/<pkg> and <pkg>)",
            details={"needs": "--config with library.package_dir"},
        )
    prov["library.package_dir"] = f"{package_dir}/__init__.py"
    init = root / package_dir / "__init__.py"
    if _VERSION_ASSIGN.search(init.read_text(encoding="utf-8", errors="replace")):
        version_file, version_pattern = f"{package_dir}/__init__.py", None
        prov["library.version_file"] = "__version__ in the package"
    elif static_version:
        version_file = "pyproject.toml"
        version_pattern = r'(?m)^version\s*=\s*"([^"]+)"'
        prov["library.version_file"] = "static version in pyproject.toml"
    else:
        version_file, version_pattern = None, None
        prov["library.version_file"] = "none found (dynamic version): the version isn't bumped"
    tag_prefix, prov["library.tag_prefix"] = _tag_prefix(git)
    source_ref, prov["library.source_ref"] = _default_branch(git)
    state = state_dir_for(root)
    prov["state_dir"] = "<git-common-dir>/lro-state (local, never committed)"

    top = package_dir.rsplit("/", 1)[-1]
    discovery = discover_consumers(
        root,
        library=dist,
        top_package=top,
        own_repository=own_repository,
        workspace=workspace or [root.parent],
        store=McpStore(state / "mcp"),
        extra=extra_consumers or [],
    )
    if discovery.question:
        questions.append(discovery.question)
    consumers: list[dict[str, Any]] = []
    for ev in discovery.confirmed():
        if ev.local_path is None or ev.declaration is None:
            continue  # remote-only: impact is reported, but no local update is possible
        consumer_git = Git(Path(ev.local_path))
        branch, _ = _default_branch(consumer_git)
        consumers.append(
            {
                "name": Path(ev.local_path).name,
                "repo": ev.local_path,
                "dependency_file": ev.declaration.file,
                "source_ref": branch,
            }
        )
    prov["consumers"] = "README-first discovery (confirmed local repositories only)"

    allow: list[str] = []
    for path in [root, *(Path(c["repo"]) for c in consumers)]:
        try:
            allow.append(Git(path).remote_url("origin"))
        except GitError:
            continue
    prov["remote_allowlist"] = "the 'origin' remotes of the library and its local consumers"

    library: dict[str, Any] = {
        "name": dist,
        "repo": str(root),
        "package_dir": package_dir,
        "source_ref": source_ref,
        "tag_prefix": tag_prefix,
    }
    if version_file:
        library["version_file"] = version_file
    if version_pattern:
        library["version_pattern"] = version_pattern
    trusted = load_trusted_validation(state)
    for consumer in consumers:
        if trusted.get(consumer["name"]):
            consumer["validation"] = trusted[consumer["name"]]
    raw: dict[str, Any] = {
        "schema_version": 1,
        "library": library,
        "registry": {"provider": "local", "path": str(state / "registry")},
        "pull_requests": {"provider": "external"},
        "state_dir": str(state),
        "consumers": consumers,
        "remote_allowlist": allow,
    }
    if trusted.get("library"):
        raw["validation"] = {"library": trusted["library"]}
        prov["validation.library"] = "confirmed by the engineer (trust-validation)"
    else:
        prov["validation.library"] = "not configured: plans stay blocked until confirmed"
        suggestion = suggest_validation(root, package_dir)
        questions.append(
            {
                "id": "validation",
                "question": "No trusted validation command for the library. Confirm one "
                "(it will run on every release branch):",
                "suggested": suggestion,
                "confirm_with": "trust-validation --scope library --name unit-tests "
                "--approve -- " + " ".join(suggestion["argv"]),
            }
        )
    config_path = state / "runtime-config.json"
    config = parse_config(raw, base_dir=root, config_path=config_path)
    result = Bootstrap(config, raw, prov, discovery, questions)
    try:
        state.mkdir(parents=True, exist_ok=True)
        tmp = config_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(result.to_dict(), indent=2, default=str), encoding="utf-8")
        os.replace(tmp, config_path)
    except OSError:
        pass  # the audit copy is best effort; the in-memory config is authoritative
    return result
