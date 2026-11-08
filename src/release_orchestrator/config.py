"""Trusted orchestrator configuration (``release-orchestrator.yaml``).

The config file is the *only* source of executable validation commands and repository
locations. It is loaded and validated eagerly so that misconfiguration fails before any
Git operation runs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from release_orchestrator.errors import ConfigError

SUPPORTED_SCHEMA_VERSIONS = {1}
ALLOWED_PLACEHOLDERS = {"python", "library_path"}
_PLACEHOLDER_RE = re.compile(r"\{([^{}]*)\}")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
DEFAULT_CHANGE_KEY_PATTERN = r"[A-Z][A-Z0-9]+-\d+"


@dataclass(frozen=True)
class CommandSpec:
    """An allowlisted validation command. ``argv`` is never passed through a shell."""

    name: str
    argv: tuple[str, ...]
    timeout_seconds: int = 600
    env: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class LibraryConfig:
    name: str
    repo: Path
    package_dir: str
    source_ref: str = "main"
    base_ref: str | None = None
    tag_prefix: str = "v"
    release_branch_prefix: str = "release/"
    version_file: str | None = None
    version_pattern: str = r'__version__\s*=\s*"([^"]+)"'


@dataclass(frozen=True)
class ConsumerConfig:
    name: str
    repo: Path
    dependency_file: str = "requirements.txt"
    source_ref: str = "main"
    branch_prefix: str = "deps/"
    validation: tuple[CommandSpec, ...] = ()


@dataclass(frozen=True)
class EnvironmentConfig:
    name: str
    required_approvals: int = 1
    allow_prerelease: bool = True


@dataclass(frozen=True)
class Config:
    config_path: Path
    library: LibraryConfig
    change_key_pattern: str
    issue_tracker_fixture: Path | None
    registry_path: Path
    pr_provider: str
    github_repositories: dict[str, str]
    github_api_url: str
    library_validation: tuple[CommandSpec, ...]
    consumers: tuple[ConsumerConfig, ...]
    state_dir: Path
    aliases: dict[str, str] = field(default_factory=dict)
    environments: tuple[EnvironmentConfig, ...] = ()
    remote_allowlist: tuple[str, ...] = ()
    remote_name: str = "origin"

    def consumer(self, name: str) -> ConsumerConfig:
        for consumer in self.consumers:
            if consumer.name == name:
                return consumer
        raise ConfigError(f"Unknown consumer '{name}'", details={"consumer": name})


def load_config(path: Path) -> Config:
    path = path.resolve()
    if not path.is_file():
        raise ConfigError(f"Config file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Config is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("Config root must be a mapping")
    return parse_config(raw, base_dir=path.parent, config_path=path)


def parse_config(raw: dict[str, Any], *, base_dir: Path, config_path: Path) -> Config:
    version = raw.get("schema_version")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise ConfigError(
            f"Unsupported config schema_version {version!r}",
            details={"supported": sorted(SUPPORTED_SCHEMA_VERSIONS)},
        )

    lib = _mapping(raw, "library")
    library = LibraryConfig(
        name=_name(lib, "name"),
        repo=_path(lib, "repo", base_dir),
        package_dir=_str(lib, "package_dir"),
        source_ref=_str(lib, "source_ref", "main"),
        base_ref=_opt_str(lib, "base_ref"),
        tag_prefix=_str(lib, "tag_prefix", "v"),
        release_branch_prefix=_str(lib, "release_branch_prefix", "release/"),
        version_file=_opt_str(lib, "version_file"),
        version_pattern=_str(lib, "version_pattern", LibraryConfig.version_pattern),
    )
    _compile(library.version_pattern, "library.version_pattern", groups=1)

    pattern = _str(raw, "change_key_pattern", DEFAULT_CHANGE_KEY_PATTERN)
    _compile(pattern, "change_key_pattern", groups=0)

    tracker = raw.get("issue_tracker") or {}
    if not isinstance(tracker, dict):
        raise ConfigError("issue_tracker must be a mapping")
    fixture = _path(tracker, "fixture", base_dir) if "fixture" in tracker else None

    registry = _mapping(raw, "registry")
    if registry.get("provider", "local") != "local":
        raise ConfigError("Only registry.provider 'local' is implemented in this version")

    prs = raw.get("pull_requests") or {}
    if not isinstance(prs, dict):
        raise ConfigError("pull_requests must be a mapping")
    pr_provider = str(prs.get("provider", "local"))
    if pr_provider not in {"local", "github", "external"}:
        raise ConfigError(f"Unknown pull_requests.provider '{pr_provider}'")
    gh_repos = prs.get("github_repositories") or {}
    if not isinstance(gh_repos, dict):
        raise ConfigError("pull_requests.github_repositories must be a mapping")

    validation = raw.get("validation") or {}
    if not isinstance(validation, dict):
        raise ConfigError("validation must be a mapping")

    consumers_raw = raw.get("consumers") or []
    if not isinstance(consumers_raw, list):
        raise ConfigError("consumers must be a list")
    consumers = tuple(_consumer(item, base_dir) for item in consumers_raw)
    names = [c.name for c in consumers]
    if len(names) != len(set(names)):
        raise ConfigError("Consumer names must be unique")

    aliases = raw.get("aliases") or {}
    if not isinstance(aliases, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in aliases.items()
    ):
        raise ConfigError("aliases must map strings to change keys")

    return Config(
        config_path=config_path,
        library=library,
        change_key_pattern=pattern,
        issue_tracker_fixture=fixture,
        registry_path=_path(registry, "path", base_dir),
        pr_provider=pr_provider,
        github_repositories={str(k): str(v) for k, v in gh_repos.items()},
        github_api_url=str(prs.get("api_url", "https://api.github.com")),
        library_validation=_commands(validation.get("library"), "validation.library"),
        consumers=consumers,
        state_dir=_path(raw, "state_dir", base_dir, default=".lro-state"),
        aliases={k.strip().lower(): v for k, v in aliases.items()},
        environments=_environments(raw.get("environments")),
        remote_allowlist=_allowlist(raw.get("remote_allowlist")),
        remote_name=_str(raw, "remote_name", "origin"),
    )


def _consumer(item: Any, base_dir: Path) -> ConsumerConfig:
    if not isinstance(item, dict):
        raise ConfigError("Each consumer must be a mapping")
    name = _name(item, "name")
    return ConsumerConfig(
        name=name,
        repo=_path(item, "repo", base_dir),
        dependency_file=_str(item, "dependency_file", "requirements.txt"),
        source_ref=_str(item, "source_ref", "main"),
        branch_prefix=_str(item, "branch_prefix", "deps/"),
        validation=_commands(item.get("validation"), f"consumers.{name}.validation"),
    )


def _commands(raw: Any, where: str) -> tuple[CommandSpec, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError(f"{where} must be a list of commands")
    specs = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ConfigError(f"{where}: each command must be a mapping")
        argv = entry.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            raise ConfigError(f"{where}: 'argv' must be a non-empty list of strings")
        env = entry.get("env") or {}
        if not isinstance(env, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in env.items()
        ):
            raise ConfigError(f"{where}: 'env' must map strings to strings")
        for arg in [*argv, *env.values()]:
            for placeholder in _PLACEHOLDER_RE.findall(arg):
                if placeholder not in ALLOWED_PLACEHOLDERS:
                    raise ConfigError(
                        f"{where}: placeholder '{{{placeholder}}}' is not allowed",
                        details={"allowed": sorted(ALLOWED_PLACEHOLDERS)},
                    )
        timeout = entry.get("timeout_seconds", 600)
        if not isinstance(timeout, int) or not 1 <= timeout <= 3600:
            raise ConfigError(f"{where}: timeout_seconds must be an integer in [1, 3600]")
        specs.append(
            CommandSpec(
                name=_name(entry, "name"),
                argv=tuple(argv),
                timeout_seconds=timeout,
                env=tuple(sorted(env.items())),
            )
        )
    return tuple(specs)


def _mapping(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise ConfigError(f"'{key}' must be a mapping")
    return value


def _str(raw: dict[str, Any], key: str, default: str | None = None) -> str:
    value = raw.get(key, default)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"'{key}' must be a non-empty string")
    return value


def _opt_str(raw: dict[str, Any], key: str) -> str | None:
    return None if raw.get(key) is None else _str(raw, key)


def _name(raw: dict[str, Any], key: str) -> str:
    value = _str(raw, key)
    if not _NAME_RE.match(value):
        raise ConfigError(f"'{key}' value '{value}' is not a valid name")
    return value


def _path(raw: dict[str, Any], key: str, base_dir: Path, default: str | None = None) -> Path:
    value = _str(raw, key, default)
    path = Path(value)
    return (path if path.is_absolute() else base_dir / path).resolve()


def _compile(pattern: str, where: str, *, groups: int) -> None:
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        raise ConfigError(f"{where} is not a valid regex: {exc}") from exc
    if compiled.groups != groups:
        raise ConfigError(f"{where} must contain exactly {groups} capture group(s)")


def normalize_remote(url: str) -> str:
    """``https://github.com/o/r.git`` / ``git@github.com:o/r.git`` -> ``github.com/o/r``."""
    text = url.strip()
    if text.lower().startswith("file://"):
        # Local bare repositories (used by the automated push tests).
        return "file://" + re.sub(r"\.git$", "", text[7:].rstrip("/")).lower()
    text = re.sub(r"^[a-z+]+://", "", text)
    text = re.sub(r"^[^@/]+@", "", text)  # drop userinfo
    text = text.replace(":", "/", 1) if re.match(r"^[^/]+:[^/]", text) else text
    text = re.sub(r"\.git$", "", text.rstrip("/"))
    return text.lower()


def _allowlist(raw: Any) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(x, str) and x for x in raw):
        raise ConfigError("remote_allowlist must be a list of repository URLs")
    entries = tuple(normalize_remote(x) for x in raw)
    for entry in entries:
        if not entry.startswith("file://") and entry.count("/") != 2:
            raise ConfigError(f"remote_allowlist entry '{entry}' must be host/owner/repo")
    return entries


def _environments(raw: Any) -> tuple[EnvironmentConfig, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError("environments must be an ordered list")
    envs = []
    for item in raw:
        if not isinstance(item, dict):
            raise ConfigError("each environment must be a mapping")
        approvals = item.get("required_approvals", 1)
        if not isinstance(approvals, int) or not 1 <= approvals <= 10:
            raise ConfigError("required_approvals must be an integer in [1, 10]")
        allow = item.get("allow_prerelease", True)
        if not isinstance(allow, bool):
            raise ConfigError("allow_prerelease must be a boolean")
        envs.append(EnvironmentConfig(_name(item, "name"), approvals, allow))
    if len({e.name for e in envs}) != len(envs):
        raise ConfigError("environment names must be unique")
    return tuple(envs)
