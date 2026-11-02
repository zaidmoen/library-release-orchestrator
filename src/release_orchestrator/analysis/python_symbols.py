"""Python language adapter: static symbol and import facts from source text.

Source is parsed with ``ast`` and never imported or executed. The facts are intentionally
simple and conservative; they are evidence for the dependency engine, not proof.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

# (module, name) pairs. ``name`` is a top-level symbol or ``Class.method``.
Symbol = tuple[str, str]


def module_name(path: str, package_dir: str) -> str | None:
    """Map ``src/pkg/sub/mod.py`` to ``pkg.sub.mod`` given ``package_dir='src/pkg'``.

    Files outside ``package_dir`` (tests, tooling) get a pseudo-module named after their
    path so that their own bindings can still be tracked.
    """
    if not path.endswith(".py"):
        return None
    package_dir = package_dir.strip("/")
    root = package_dir.rsplit("/", 1)[0] if "/" in package_dir else ""
    rel = path[len(root) + 1 :] if root and path.startswith(root + "/") else path
    parts = rel[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


@dataclass
class ModuleFacts:
    module: str
    package: str = ""
    definitions: dict[str, str] = field(default_factory=dict)  # name -> ast dump
    bindings: dict[str, tuple[str, str | None]] = field(default_factory=dict)
    imported_modules: set[str] = field(default_factory=set)


def parse(source: str) -> ast.Module:
    return ast.parse(source)


def module_facts(tree: ast.Module, module: str, *, is_package: bool = False) -> ModuleFacts:
    package = module if is_package else (module.rsplit(".", 1)[0] if "." in module else "")
    facts = ModuleFacts(module=module, package=package)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            facts.definitions[node.name] = ast.dump(node)
            facts.bindings[node.name] = (module, node.name)
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
                        facts.definitions[f"{node.name}.{item.name}"] = ast.dump(item)
        elif isinstance(node, ast.Assign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    facts.definitions[target.id] = ast.dump(node)
                    facts.bindings[target.id] = (module, target.id)
    for walked in ast.walk(tree):
        if isinstance(walked, ast.Import):
            for alias in walked.names:
                facts.imported_modules.add(alias.name)
                if alias.asname:
                    facts.bindings[alias.asname] = (alias.name, None)
                else:
                    top = alias.name.split(".")[0]
                    facts.bindings[top] = (top, None)
        elif isinstance(walked, ast.ImportFrom):
            base = _resolve_from(walked, package)
            facts.imported_modules.add(base)
            for alias in walked.names:
                if alias.name == "*":
                    continue
                bound = alias.asname or alias.name
                # `from pkg import sub` may bind a submodule; record both interpretations.
                facts.bindings[bound] = (base, alias.name)
                facts.imported_modules.add(f"{base}.{alias.name}")
    return facts


def _resolve_from(node: ast.ImportFrom, package: str) -> str:
    if not node.level:
        return node.module or ""
    parts = package.split(".") if package else []
    if node.level > 1:
        parts = parts[: len(parts) - (node.level - 1)]
    if node.module:
        parts.append(node.module)
    return ".".join(parts)


@dataclass(frozen=True)
class Reference:
    """A name used on a changed line, resolved through the file's bindings if possible."""

    name: str
    target: Symbol | None  # (module, attr) if resolvable
    line: int


def references_on_lines(tree: ast.Module, facts: ModuleFacts, lines: set[int]) -> list[Reference]:
    refs: list[Reference] = []
    for node in ast.walk(tree):
        lineno = getattr(node, "lineno", None)
        end = getattr(node, "end_lineno", lineno)
        if lineno is None or end is None:
            continue
        if not any(lineno <= ln <= end for ln in lines):
            continue
        if isinstance(node, ast.Name):
            refs.append(Reference(node.id, _resolve_name(node.id, facts), node.lineno))
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            binding = facts.bindings.get(node.value.id)
            if binding is not None:
                module = binding[0] if binding[1] is None else f"{binding[0]}.{binding[1]}"
                refs.append(Reference(node.attr, (module, node.attr), node.lineno))
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_from(node, facts.package)
            for alias in node.names:
                refs.append(Reference(alias.name, (base, alias.name), node.lineno))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                refs.append(Reference(alias.name, (alias.name, ""), node.lineno))
    return refs


def _resolve_name(name: str, facts: ModuleFacts) -> Symbol | None:
    binding = facts.bindings.get(name)
    if binding is None:
        return None
    module, attr = binding
    return (module, attr if attr is not None else "")


def public_names(facts: ModuleFacts) -> set[str]:
    return {n for n in facts.definitions if "." not in n and not n.startswith("_")}
