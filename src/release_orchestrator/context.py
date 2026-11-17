"""Automatic repository context discovery (ADR-0015).

``/library-release`` invoked from inside a library repository must know, without being
told: which repository it is in, its GitHub ``owner/name``, the current branch and HEAD,
the default branch, which trusted config describes it, and the release tags it carries.

Every value records **where it came from** (``*_source``). Nothing is guessed:

* the repository is the Git top-level of the working directory (or the library path of a
  config found by walking up from it);
* ``owner/name`` comes from the Git remote, an explicit ``--repository`` override, or the
  config's ``pull_requests.github_repositories`` entry, in that order, and a disagreement
  between them is reported;
* a config is used only if its ``library.repo`` *is* this repository. A config describing
  another repository is reported as ``CONFIG_MISMATCH`` and never silently used.

When something can't be determined, ``problems`` says exactly what is missing and which
single input would resolve it (``needs``). Discovery is read-only: ``git rev-parse``,
``git symbolic-ref``, ``git remote get-url``, ``git tag -l``, optionally ``git ls-remote
--symref`` and one GitHub ``GET /repos/{owner}/{name}``.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from release_orchestrator.config import Config, load_config, normalize_remote
from release_orchestrator.errors import ConfigError, GitError, IntegrationError, OrchestratorError
from release_orchestrator.gitutil.repo import Git
from release_orchestrator.versioning.semver import SemVer

CONFIG_NAMES = (
    "release-orchestrator.yaml",
    ".release-orchestrator.yaml",
    ".github/release-orchestrator.yaml",
)
_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$")


class RepositoryReader(Protocol):
    def repository(self, repository: str) -> dict[str, Any]: ...


@dataclass
class Problem:
    code: str
    message: str
    needs: str  # the single input that resolves it

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass
class RepoContext:
    cwd: str
    root: str | None = None
    root_source: str = "unresolved"
    remote_name: str = "origin"
    remote_url: str | None = None
    host: str | None = None
    repository: str | None = None  # owner/name
    repository_source: str = "unresolved"
    branch: str | None = None
    head: str | None = None
    default_branch: str | None = None
    default_branch_source: str = "unresolved"
    config_path: str | None = None
    config_source: str = "not found"
    library: str | None = None
    tag_prefix: str = "v"
    latest_stable: dict[str, str] | None = None
    release_candidates: list[dict[str, str]] = field(default_factory=list)
    stable_tags: int = 0
    problems: list[Problem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["ok"] = self.ok
        data["problems"] = [p.to_dict() for p in self.problems]
        return data


def parse_remote(url: str) -> tuple[str, str] | None:
    """``(host, owner/name)`` for a hosted remote URL; None for local paths or odd shapes."""
    normalized = normalize_remote(url)
    if normalized.startswith("file://") or normalized.startswith("/"):
        return None
    parts = normalized.split("/")
    if len(parts) != 3 or not all(parts) or "." not in parts[0]:
        return None
    return parts[0], f"{parts[1]}/{parts[2]}"


def validate_repository(value: str) -> str:
    if not _REPO_RE.match(value) or value.endswith(".git"):
        raise ConfigError(f"--repository must look like 'owner/name', got {value!r}")
    return value


def find_configs(start: Path, levels: int = 3) -> list[Path]:
    """Config files in ``start`` and up to ``levels`` parent directories (nearest first)."""
    found: list[Path] = []
    directory = start.resolve()
    for _ in range(levels + 1):
        found += [directory / n for n in CONFIG_NAMES if (directory / n).is_file()]
        if directory.parent == directory:
            break
        directory = directory.parent
    return found


def _toplevel(path: Path) -> Path | None:
    if not path.is_dir():
        return None
    proc = Git(path).run("rev-parse", "--show-toplevel", check=False)
    return Path(proc.stdout.strip()).resolve() if proc.returncode == 0 else None


def _same(a: Path, b: Path) -> bool:
    try:
        return a.resolve().samefile(b.resolve())
    except OSError:
        return False


def _select_config(
    ctx: RepoContext, cwd: Path, root: Path | None, explicit: Path | None
) -> Config | None:
    if explicit is not None:
        config = load_config(explicit)  # an explicit config that can't load is an error
        ctx.config_source = "explicit (--config / $LRO_CONFIG)"
        if root is not None and not _same(config.library.repo, root):
            ctx.problems.append(
                Problem(
                    "CONFIG_MISMATCH",
                    f"{explicit} describes library '{config.library.name}' at "
                    f"{config.library.repo}, but you are in {root}",
                    "run from the library repository, or pass the config for this repository",
                )
            )
        return config
    mismatched: list[str] = []
    for path in find_configs(root or cwd):
        try:
            config = load_config(path)
        except ConfigError as exc:
            ctx.warnings.append(f"ignored invalid config {path}: {exc.message}")
            continue
        if root is None or _same(config.library.repo, root):
            ctx.config_source = f"discovered ({path.name}, nearest matching)"
            return config
        mismatched.append(f"{path} -> {config.library.repo}")
    if mismatched:
        ctx.problems.append(
            Problem(
                "CONFIG_MISMATCH",
                "release configs were found nearby but none describes this repository: "
                + "; ".join(mismatched),
                "--config <file> for this repository",
            )
        )
    return None


def _default_branch(
    ctx: RepoContext, git: Git, config: Config | None, reader: RepositoryReader | None
) -> None:
    remote = ctx.remote_name
    proc = git.run("symbolic-ref", "--short", "-q", f"refs/remotes/{remote}/HEAD", check=False)
    if proc.returncode == 0 and proc.stdout.strip():
        ctx.default_branch = proc.stdout.strip().split("/", 1)[-1]
        ctx.default_branch_source = f"git (refs/remotes/{remote}/HEAD)"
        return
    if ctx.remote_url:
        try:
            out = git.out("ls-remote", "--symref", remote, "HEAD")
            match = re.search(r"^ref: refs/heads/(\S+)\s+HEAD", out, re.M)
            if match:
                ctx.default_branch = match.group(1)
                ctx.default_branch_source = f"git (ls-remote --symref {remote} HEAD)"
                return
        except (GitError, IntegrationError) as exc:
            ctx.warnings.append(f"remote HEAD unreadable: {exc.message}")
    if reader is not None and ctx.repository and ctx.host == "github.com":
        try:
            branch = reader.repository(ctx.repository).get("default_branch")
            if branch:
                ctx.default_branch = str(branch)
                ctx.default_branch_source = "github api (repository.default_branch)"
                return
        except OrchestratorError as exc:
            ctx.warnings.append(f"GitHub repository metadata unreadable: {exc.message}")
    if config is not None:
        ctx.default_branch = config.library.source_ref
        ctx.default_branch_source = "config (library.source_ref); remote default not verified"
        ctx.warnings.append(
            f"default branch taken from config ({config.library.source_ref}); "
            "the remote's default branch could not be read"
        )
        return
    ctx.problems.append(
        Problem(
            "DEFAULT_BRANCH_UNKNOWN",
            "the default branch could not be read from Git, the remote or GitHub",
            "--config with library.source_ref",
        )
    )


def _tags(ctx: RepoContext, git: Git) -> None:
    versions: dict[SemVer, str] = {}
    for tag in git.tags(f"{ctx.tag_prefix}*"):
        parsed = SemVer.try_parse(tag[len(ctx.tag_prefix) :])
        if parsed is not None:
            versions[parsed] = tag
    stable = sorted(v for v in versions if not v.is_prerelease)
    ctx.stable_tags = len(stable)
    if stable:
        ctx.latest_stable = {
            "version": str(stable[-1]),
            "tag": versions[stable[-1]],
            "sha": git.rev_parse(versions[stable[-1]]),
        }
    ctx.release_candidates = [
        {"version": str(v), "tag": versions[v], "sha": git.rev_parse(versions[v])}
        for v in sorted(versions)
        if v.is_prerelease and (not stable or v > stable[-1])
    ]


def discover_context(
    cwd: Path,
    *,
    config_path: Path | None = None,
    repository: str | None = None,
    reader: RepositoryReader | None = None,
) -> tuple[RepoContext, Config | None]:
    """Discover the release context of ``cwd``. Returns the context and the config used."""
    cwd = cwd.resolve()
    ctx = RepoContext(cwd=str(cwd))
    root = _toplevel(cwd)
    config = _select_config(ctx, cwd, root, config_path)
    if root is not None:
        ctx.root, ctx.root_source = str(root), "git (rev-parse --show-toplevel)"
    elif config is not None and _toplevel(config.library.repo) is not None:
        root = _toplevel(config.library.repo)
        ctx.root, ctx.root_source = str(root), "config (library.repo)"
    else:
        ctx.problems.append(
            Problem(
                "NOT_A_GIT_REPOSITORY",
                f"{cwd} is not inside a Git repository and no release config was found",
                "run /library-release inside the library repository, or pass --config",
            )
        )
        return ctx, config
    if config is not None:
        ctx.config_path = str(config.config_path)
        ctx.library = config.library.name
        ctx.tag_prefix = config.library.tag_prefix
        ctx.remote_name = config.remote_name
    git = Git(Path(ctx.root))

    try:
        ctx.remote_url = git.remote_url(ctx.remote_name)
    except GitError:
        ctx.warnings.append(f"no '{ctx.remote_name}' remote configured in {root}")
    parsed = parse_remote(ctx.remote_url) if ctx.remote_url else None
    if parsed:
        ctx.host, ctx.repository = parsed
        ctx.repository_source = f"git remote '{ctx.remote_name}'"
    if repository:
        override = validate_repository(repository)
        if ctx.repository and ctx.repository.lower() != override.lower():
            ctx.warnings.append(
                f"--repository {override} differs from the Git remote ({ctx.repository}); "
                "the override is used as requested"
            )
        ctx.repository, ctx.repository_source = override, "explicit override (--repository)"
        ctx.host = ctx.host or "github.com"
    if ctx.repository is None and config is not None:
        declared = config.github_repositories.get(config.library.name)
        if declared:
            ctx.repository = validate_repository(declared)
            ctx.host = "github.com"
            ctx.repository_source = "config (pull_requests.github_repositories)"
    if ctx.repository is None:
        ctx.problems.append(
            Problem(
                "REPOSITORY_UNRESOLVED",
                "the GitHub repository could not be determined: "
                + (
                    f"remote '{ctx.remote_name}' is {ctx.remote_url!r}, which is not a hosted URL"
                    if ctx.remote_url
                    else f"there is no '{ctx.remote_name}' remote"
                ),
                "--repository owner/name",
            )
        )

    branch = git.run("symbolic-ref", "--short", "-q", "HEAD", check=False)
    ctx.branch = branch.stdout.strip() or None
    if ctx.branch is None:
        ctx.warnings.append("HEAD is detached (no current branch)")
    ctx.head = git.try_rev_parse("HEAD")
    _default_branch(ctx, git, config, reader)
    _tags(ctx, git)
    if config is None and not any(p.code == "CONFIG_MISMATCH" for p in ctx.problems):
        ctx.problems.append(
            Problem(
                "CONFIG_NOT_FOUND",
                f"no {CONFIG_NAMES[0]} found in {root} or its parents; services, "
                "environments and policies are unknown",
                "--config <file>",
            )
        )
    return ctx, config
