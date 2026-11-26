"""Read a consumer's declared dependency on the library from its manifests (ADR-0017).

Supported (read-only):

* ``requirements*.txt``: ``name==1.2.3``, ``name>=1.2``, extras and markers;
* ``pyproject.toml``: PEP 621 ``[project].dependencies`` and Poetry
  ``[tool.poetry.dependencies]`` (string or table with ``version``);
* ``setup.cfg``: ``[options] install_requires``.

Anything else (lock files, ``setup.py`` code, Pipfile) is reported as **unsupported**,
never guessed. Manifest text is untrusted data: it's parsed, never executed.
"""

from __future__ import annotations

import configparser
import re
import tomllib
from dataclasses import asdict, dataclass
from typing import Any

from release_orchestrator.consumers.discovery import find_requirement, normalize

SUPPORTED_FILES = ("requirements.txt", "pyproject.toml", "setup.cfg")
UNSUPPORTED_FILES = ("poetry.lock", "uv.lock", "Pipfile", "Pipfile.lock", "setup.py")
_PEP508 = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*(.*)$")
_SPEC = re.compile(r"(===|==|~=|!=|<=|>=|<|>|\^|~)\s*([^\s,;]+)")


@dataclass(frozen=True)
class Declaration:
    """How one manifest declares the library."""

    file: str
    format: str  # requirements | pep621 | poetry | setup.cfg
    specifier: str  # e.g. "==1.0.0", ">=1.0,<2", "^1.2", "" (unconstrained)
    operator: str | None  # first operator found, or None
    version: str | None  # version of that operator
    pinned: bool  # exactly one "==" (the only form updated automatically)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _parse_spec(file: str, fmt: str, spec: str) -> Declaration:
    spec = spec.split(";", 1)[0].strip()
    found = _SPEC.findall(spec)
    operator, version = (found[0][0], found[0][1]) if found else (None, None)
    pinned = len(found) == 1 and operator == "=="
    return Declaration(file, fmt, spec, operator, version, pinned)


def from_requirements(file: str, text: str, library: str) -> Declaration | None:
    req = find_requirement(text, library)
    if req is None:
        return None
    line = text.splitlines()[req.line_no]
    match = _PEP508.match(line.split("#", 1)[0])
    return _parse_spec(file, "requirements", match.group(2) if match else "")


def from_pyproject(file: str, text: str, library: str) -> Declaration | None:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None
    target = normalize(library)
    for entry in (data.get("project") or {}).get("dependencies") or []:
        match = _PEP508.match(str(entry))
        if match and normalize(match.group(1)) == target:
            return _parse_spec(file, "pep621", match.group(2))
    poetry = ((data.get("tool") or {}).get("poetry") or {}).get("dependencies") or {}
    for name, value in poetry.items():
        if normalize(str(name)) != target:
            continue
        version = value.get("version", "") if isinstance(value, dict) else str(value)
        spec = str(version).strip()
        if re.fullmatch(r"\d[\w.]*", spec):
            spec = f"=={spec}"  # Poetry: a bare version is an exact pin
        return _parse_spec(file, "poetry", "" if spec == "*" else spec)
    return None


def from_setup_cfg(file: str, text: str, library: str) -> Declaration | None:
    parser = configparser.ConfigParser()
    try:
        parser.read_string(text)
    except configparser.Error:
        return None
    raw = parser.get("options", "install_requires", fallback="")
    for line in raw.splitlines():
        match = _PEP508.match(line)
        if line.strip() and match and normalize(match.group(1)) == normalize(library):
            return _parse_spec(file, "setup.cfg", match.group(2))
    return None


def find_declaration(files: dict[str, str], library: str) -> Declaration | None:
    """The first declaration of ``library`` across supported manifest files."""
    for name in sorted(files, key=_priority):
        base = name.rsplit("/", 1)[-1]
        text = files[name]
        if base.startswith("requirements") and base.endswith(".txt"):
            found = from_requirements(name, text, library)
        elif base == "pyproject.toml":
            found = from_pyproject(name, text, library)
        elif base == "setup.cfg":
            found = from_setup_cfg(name, text, library)
        else:
            continue
        if found is not None:
            return found
    return None


def manifest_candidates(paths: list[str]) -> tuple[list[str], list[str]]:
    """Split repository paths into supported manifests and unsupported ones present."""
    supported, unsupported = [], []
    for path in paths:
        if path.count("/") > 1:
            continue  # manifests at the root or one level down only
        base = path.rsplit("/", 1)[-1]
        if (base.startswith("requirements") and base.endswith(".txt")) or base in (
            "pyproject.toml",
            "setup.cfg",
        ):
            supported.append(path)
        elif base in UNSUPPORTED_FILES:
            unsupported.append(path)
    return sorted(supported, key=_priority), sorted(unsupported)


def _priority(path: str) -> tuple[int, str]:
    base = path.rsplit("/", 1)[-1]
    order = {"requirements.txt": 0, "pyproject.toml": 1, "setup.cfg": 2}
    return (order.get(base, 3), path)
