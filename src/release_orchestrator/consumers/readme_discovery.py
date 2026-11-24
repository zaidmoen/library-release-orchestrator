"""README-first consumer discovery (ADR-0017).

1. **Read the library README** and extract *candidate* consumers: GitHub repository URLs
   and items listed under consumer-like headings ("Consumers", "Used by", "Dependent
   services"...). README text is untrusted: it only proposes names.
2. **Verify each candidate** against the candidate repository's real manifests:
   * a local clone found in the workspace (its ``origin`` must match a URL when the README
     gives one), with import analysis; or
   * GitHub MCP ``get_file_contents`` evidence (``mcp-ingest --kind github-file``), with
     the declaration only (imports aren't visible without the code).
3. **Classify**, never assume:

   ============== ==========================================================
   CONFIRMED      a supported manifest declares the library
   NOT_A_CONSUMER supported manifests were read and none declares it
   POSSIBLE       a GitHub search hit, not yet verified
   INACCESSIBLE   the repository can't be read (no clone, no MCP evidence)
   UNKNOWN        readable, but only unsupported manifest formats
   ============== ==========================================================

   An inaccessible or unknown repository is **never** treated as unaffected, and
   discovery reports itself incomplete.
4. If the README lists no consumers, discovery returns the agreed question (search
   GitHub, give names, or continue with acknowledged incomplete coverage) and targeted
   GitHub code-search queries, never an org-wide crawl.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from release_orchestrator.config import normalize_remote
from release_orchestrator.consumers.discovery import library_imports, normalize
from release_orchestrator.consumers.manifests import (
    Declaration,
    find_declaration,
    manifest_candidates,
)
from release_orchestrator.errors import GitError
from release_orchestrator.gitutil.repo import Git
from release_orchestrator.integrations.mcp_snapshot import McpStore

CONFIRMED, POSSIBLE, NOT_A_CONSUMER, INACCESSIBLE, UNKNOWN = (
    "CONFIRMED",
    "POSSIBLE",
    "NOT_A_CONSUMER",
    "INACCESSIBLE",
    "UNKNOWN",
)
README_NAMES = ("README.md", "README.rst", "README.txt", "README", "readme.md")
MISSING_QUESTION = (
    "لم أجد قائمة واضحة بالـ Consumer Services في README. بدك أبحث عنها عبر GitHub، "
    "أو تعطيني أسماء الـ Repositories؟"
)
_GITHUB_URL = re.compile(
    r"https?://(?:www\.)?github\.com/([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9._-]{1,100})"
)
_CONSUMER_WORDS = re.compile(
    r"\b(consumers?|consuming|used\s+by|dependents?|downstream|services\s+using|"
    r"who\s+uses|clients?\s+of)\b",
    re.I,
)
_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*\S)\s*$")
_REPO_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


@dataclass(frozen=True)
class Candidate:
    name: str  # display name as written in the README
    slugs: tuple[str, ...]  # repository names to look for
    repository: str | None  # owner/name when the README gives a GitHub URL
    line: int
    source: str = "README"


@dataclass
class ConsumerEvidence:
    name: str
    status: str
    repository: str | None = None
    local_path: str | None = None
    revision: str | None = None
    declaration: Declaration | None = None
    imports: list[str] | None = None  # None = not inspected (no code access)
    evidence: list[str] = field(default_factory=list)
    discovered_by: str = "README"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["declaration"] = self.declaration.to_dict() if self.declaration else None
        return data


@dataclass
class DiscoveryResult:
    readme: str | None
    candidates: list[Candidate]
    consumers: list[ConsumerEvidence]
    complete: bool
    question: dict[str, Any] | None
    search_plan: list[str]
    notes: list[str] = field(default_factory=list)

    def confirmed(self) -> list[ConsumerEvidence]:
        return [c for c in self.consumers if c.status == CONFIRMED]

    def to_dict(self) -> dict[str, Any]:
        return {
            "readme": self.readme,
            "candidates": [asdict(c) for c in self.candidates],
            "consumers": [c.to_dict() for c in self.consumers],
            "complete": self.complete,
            "question": self.question,
            "search_plan": self.search_plan,
            "notes": self.notes,
        }


# --------------------------------------------------------------------------- README
def find_readme(root: Path) -> Path | None:
    for name in README_NAMES:
        path = root / name
        if path.is_file():
            return path
    return None


def _slugs(text: str) -> tuple[str, ...]:
    base = text.strip().strip("`*_[]()").strip()
    variants = {base}
    if " " in base:
        variants |= {"-".join(base.split()), "_".join(base.split())}
    lowered = {v.lower() for v in variants}
    return tuple(sorted(v for v in variants | lowered if _REPO_NAME.match(v)))


def extract_candidates(text: str, *, exclude: Iterable[str] = ()) -> list[Candidate]:
    """Candidate consumers from README text (never verified here)."""
    excluded = {e.lower() for e in exclude}
    found: dict[str, Candidate] = {}

    def add(cand: Candidate) -> None:
        key = (cand.repository or (cand.slugs[0] if cand.slugs else cand.name)).lower()
        if key in excluded or key.rsplit("/", 1)[-1] in excluded or key in found:
            return
        found[key] = cand

    lines = text.splitlines()
    for number, line in enumerate(lines, 1):
        for owner, repo in _GITHUB_URL.findall(line):
            repo = re.sub(r"\.git$", "", repo).rstrip(".")
            add(Candidate(repo, (repo,), f"{owner}/{repo}", number))

    in_section = False
    for number, line in enumerate(lines, 1):
        heading = re.match(r"^\s{0,3}#{1,6}\s+(.*\S)\s*$", line)
        if heading:
            in_section = bool(_CONSUMER_WORDS.search(heading.group(1)))
            continue
        if not in_section:
            continue
        item = _ITEM.match(line)
        if not item:
            continue
        body = item.group(1)
        if _GITHUB_URL.search(body):
            continue  # already captured with its owner
        quoted = re.findall(r"`([^`]+)`", body)
        link = re.match(r"\[([^\]]+)\]", body)
        # a plain item ends at " - ", an en/em dash, ":" or "(" (its description)
        separator = r"\s[-" + chr(0x2013) + chr(0x2014) + r":(]"
        name = quoted[0] if quoted else link.group(1) if link else re.split(separator, body)[0]
        slugs = _slugs(name)
        if slugs:
            add(Candidate(name.strip(), slugs, None, number))
    return list(found.values())


# --------------------------------------------------------------------------- verification
def _local_repo(cand: Candidate, workspace: list[Path], remote: str) -> tuple[Path | None, str]:
    for directory in workspace:
        for slug in cand.slugs:
            path = directory / slug
            if not (path / ".git").exists():
                continue
            if cand.repository:
                try:
                    url = Git(path).remote_url(remote)
                except GitError:
                    return None, f"{path} has no '{remote}' remote to match {cand.repository}"
                if not normalize_remote(url).endswith("/" + cand.repository.lower()):
                    return None, f"{path} is a different repository ({normalize_remote(url)})"
            return path, ""
    return None, ""


def verify_candidate(
    cand: Candidate,
    *,
    library: str,
    top_package: str,
    workspace: list[Path],
    store: McpStore | None,
    remote: str = "origin",
) -> ConsumerEvidence:
    ev = ConsumerEvidence(cand.name, UNKNOWN, cand.repository, discovered_by=cand.source)
    ev.evidence.append(f"{cand.source} line {cand.line}: {cand.name}")
    path, note = _local_repo(cand, workspace, remote)
    if note:
        ev.evidence.append(note)
    if path is not None:
        git = Git(path)
        head = git.try_rev_parse("HEAD")
        if head is None:
            ev.status = INACCESSIBLE
            ev.evidence.append(f"{path}: no commits")
            return ev
        ev.local_path, ev.revision = str(path), head
        supported, unsupported = manifest_candidates(git.ls_files_at(head))
        files = {p: git.show_file(head, p) or "" for p in supported}
        ev.declaration = find_declaration(files, library)
        if ev.declaration is not None:
            ev.status = CONFIRMED
            ev.imports = sorted(library_imports(git, head, top_package))
            ev.evidence.append(
                f"{ev.declaration.file} declares {library} {ev.declaration.specifier or '(any)'}"
                f" at {head[:10]}"
            )
        elif supported:
            ev.status = NOT_A_CONSUMER
            ev.evidence.append(f"read {', '.join(supported)}: no {library} dependency")
        else:
            ev.status = UNKNOWN
            ev.evidence.append(
                "no supported manifest"
                + (f"; unsupported: {', '.join(unsupported)}" if unsupported else "")
            )
        return ev
    if cand.repository and store is not None:
        files = {}
        for name in ("requirements.txt", "pyproject.toml", "setup.cfg"):
            snap = store.get("github-file", cand.repository, name)
            if snap is not None:
                files[name] = snap.data["text"]
                ev.revision = snap.data.get("commit_sha")
        if files:
            ev.declaration = find_declaration(files, library)
            if ev.declaration is not None:
                ev.status = CONFIRMED
                ev.evidence.append(
                    f"GitHub MCP {ev.declaration.file} declares {library} "
                    f"{ev.declaration.specifier or '(any)'}; imports not inspected (no clone)"
                )
            else:
                ev.status = NOT_A_CONSUMER
                ev.evidence.append(f"GitHub MCP {', '.join(files)}: no {library} dependency")
            return ev
    ev.status = INACCESSIBLE
    ev.evidence.append(
        "not readable: clone it next to the library, or fetch its manifests through GitHub MCP"
    )
    return ev


def search_plan(library: str, top_package: str, owner: str | None) -> list[str]:
    """Targeted GitHub code-search queries (for the MCP ``search_code`` tool)."""
    scope = f" org:{owner}" if owner else ""
    return [
        f'"{library}" filename:requirements.txt{scope}',
        f'"{library}" filename:pyproject.toml{scope}',
        f'"import {top_package}" language:python{scope}',
    ]


def discover_consumers(
    root: Path,
    *,
    library: str,
    top_package: str,
    own_repository: str | None,
    workspace: list[Path],
    store: McpStore | None,
    extra: Iterable[str] = (),
) -> DiscoveryResult:
    readme = find_readme(root)
    exclude = {root.name, library, normalize(library)}
    if own_repository:
        exclude |= {own_repository, own_repository.rsplit("/", 1)[-1]}
    candidates = (
        extract_candidates(readme.read_text(encoding="utf-8", errors="replace"), exclude=exclude)
        if readme
        else []
    )
    for number, name in enumerate(extra, 1):  # names/URLs given by the engineer
        match = _GITHUB_URL.search(name)
        if match:
            repo = re.sub(r"\.git$", "", match.group(2))
            candidates.append(Candidate(repo, (repo,), f"{match.group(1)}/{repo}", number, "user"))
        elif _slugs(name):
            candidates.append(Candidate(name, _slugs(name), None, number, "user"))
    if store is not None:  # search hits from an authorised MCP code search are POSSIBLE
        known = {c.repository for c in candidates if c.repository}
        for snap in store.all("github-code-search"):
            for hit in snap.data:
                repo = hit["repository"]
                if repo not in known and repo != own_repository:
                    known.add(repo)
                    name = repo.rsplit("/", 1)[-1]
                    candidates.append(Candidate(name, (name,), repo, 0, "github-search"))
    consumers = [
        verify_candidate(
            c, library=library, top_package=top_package, workspace=workspace, store=store
        )
        for c in candidates
    ]
    for c, cand in zip(consumers, candidates, strict=True):
        if cand.source == "github-search" and c.status == INACCESSIBLE:
            c.status = POSSIBLE  # a search hit that couldn't be verified yet
    owner = own_repository.split("/", 1)[0] if own_repository else None
    question = None
    if not candidates:
        question = {
            "id": "consumers",
            "question": MISSING_QUESTION,
            "options": [
                "Search accessible GitHub repositories (targeted queries below)",
                "Provide repository names or URLs",
                "Continue with incomplete impact coverage (acknowledged)",
            ],
        }
    unresolved = [c for c in consumers if c.status in (INACCESSIBLE, UNKNOWN, POSSIBLE)]
    notes = [f"{c.name}: {c.status.lower()} — impact can't be ruled out" for c in unresolved]
    return DiscoveryResult(
        readme=readme.name if readme else None,
        candidates=candidates,
        consumers=consumers,
        complete=bool(candidates) and not unresolved,
        question=question,
        search_plan=search_plan(library, top_package, owner),
        notes=notes,
    )
