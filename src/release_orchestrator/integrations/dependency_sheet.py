"""Optional release-dependency tracking sheet (ADR-0015).

Some teams track "ticket X depends on ticket Y" and "ticket X ships in release Z" in a
spreadsheet. No sheet URL or schema exists in this repository, so none is assumed. This
module provides:

* ``DependencySheet``: the interface any sheet source implements;
* ``CsvDependencySheet``: a CSV export or snapshot, with configurable column names;
* ``NullDependencySheet``: used when no sheet is configured. The release workflow never
  depends on a sheet being available.

A live Google Sheets source can be added behind the same interface. Until then, a sheet can
be exported to CSV (by a human, or by the Skill through a Google Sheets connector) and
configured as ``dependency_sheet: {provider: csv, path: ...}``.

Sheet rows are **declarations**, not proof. Precedence and conflict rules are in
``release_graph`` and docs/dependency-intelligence.md. Cell text is untrusted: only ticket
keys matching the change-key pattern are used, and notes are truncated.
"""

from __future__ import annotations

import csv
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from release_orchestrator.errors import ConfigError

DEFAULT_COLUMNS = {
    "ticket": "ticket",
    "depends_on": "depends_on",
    "release": "release",
    "notes": "notes",
}


@dataclass(frozen=True)
class SheetRow:
    row: int
    ticket: str
    depends_on: tuple[str, ...] = ()
    release: str | None = None
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SheetSnapshot:
    source: str
    rows: list[SheetRow] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)  # rows that couldn't be used, and why

    def depends_on(self, key: str) -> list[tuple[str, int]]:
        return [(d, r.row) for r in self.rows if r.ticket == key for d in r.depends_on]

    def release_of(self, key: str) -> list[tuple[str, int]]:
        return [(r.release, r.row) for r in self.rows if r.ticket == key and r.release]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "rows": [r.to_dict() for r in self.rows],
            "errors": self.errors,
        }


class DependencySheet(Protocol):
    def snapshot(self) -> SheetSnapshot: ...


class NullDependencySheet:
    def snapshot(self) -> SheetSnapshot:
        return SheetSnapshot(source="not configured")


class CsvDependencySheet:
    def __init__(
        self, path: Path, *, key_pattern: str, columns: dict[str, str] | None = None
    ) -> None:
        self.path = Path(path)
        self.columns = {**DEFAULT_COLUMNS, **(columns or {})}
        self._key = re.compile(rf"^(?:{key_pattern})$")
        self._find = re.compile(key_pattern)

    def snapshot(self) -> SheetSnapshot:
        snap = SheetSnapshot(source=f"csv:{self.path.name}")
        try:
            text = self.path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise ConfigError(f"Cannot read dependency sheet {self.path}: {exc}") from exc
        reader = csv.DictReader(text.splitlines())
        header = set(reader.fieldnames or [])
        missing = {self.columns["ticket"], self.columns["depends_on"]} - header
        if missing:
            raise ConfigError(
                f"dependency sheet {self.path.name} lacks column(s) {sorted(missing)}",
                details={"columns": sorted(header), "expected": self.columns},
            )
        for number, raw in enumerate(reader, start=2):  # row 1 is the header
            ticket = (raw.get(self.columns["ticket"]) or "").strip()
            if not ticket:
                continue
            if not self._key.match(ticket):
                snap.errors.append(f"row {number}: {ticket[:40]!r} is not a ticket key")
                continue
            deps_text = raw.get(self.columns["depends_on"]) or ""
            deps = tuple(dict.fromkeys(self._find.findall(deps_text)))
            if ticket in deps:
                snap.errors.append(f"row {number}: {ticket} depends on itself")
                deps = tuple(d for d in deps if d != ticket)
            release = (raw.get(self.columns["release"]) or "").strip() or None
            snap.rows.append(
                SheetRow(
                    row=number,
                    ticket=ticket,
                    depends_on=deps,
                    release=release[:60] if release else None,
                    notes=(raw.get(self.columns["notes"]) or "").strip()[:200],
                )
            )
        return snap


def build_sheet(raw: Any, *, base_dir: Path, key_pattern: str) -> DependencySheet:
    """Build the sheet source from the optional ``dependency_sheet`` config mapping."""
    if raw is None:
        return NullDependencySheet()
    if not isinstance(raw, dict):
        raise ConfigError("dependency_sheet must be a mapping")
    provider = raw.get("provider")
    if provider != "csv":
        raise ConfigError(
            f"dependency_sheet.provider {provider!r} is not supported; use 'csv' (export the "
            "sheet, or let the Skill read it through a Google Sheets connector into a CSV)",
        )
    path = raw.get("path")
    if not isinstance(path, str) or not path:
        raise ConfigError("dependency_sheet.path is required")
    columns = raw.get("columns") or {}
    if not isinstance(columns, dict) or not set(columns) <= set(DEFAULT_COLUMNS):
        raise ConfigError(
            f"dependency_sheet.columns may only rename {sorted(DEFAULT_COLUMNS)}",
        )
    return CsvDependencySheet(
        (base_dir / path).resolve(),
        key_pattern=key_pattern,
        columns={str(k): str(v) for k, v in columns.items()},
    )
