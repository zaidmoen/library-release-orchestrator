"""Directed change-dependency graph with cycle detection."""

from __future__ import annotations

from collections.abc import Iterable


class ChangeGraph:
    """Edges point from a change to the changes it depends on."""

    def __init__(self, nodes: Iterable[str] = ()) -> None:
        self._edges: dict[str, set[str]] = {n: set() for n in nodes}

    def add_edge(self, change: str, depends_on: str) -> None:
        self._edges.setdefault(change, set()).add(depends_on)
        self._edges.setdefault(depends_on, set())

    def dependencies(self, change: str) -> set[str]:
        return set(self._edges.get(change, set()))

    def closure(self, roots: Iterable[str]) -> set[str]:
        """All changes reachable from ``roots`` (roots included)."""
        seen: set[str] = set()
        stack = list(roots)
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            stack.extend(self._edges.get(node, ()))
        return seen

    def cycles(self) -> list[list[str]]:
        """Strongly connected components with more than one node (Tarjan)."""
        index: dict[str, int] = {}
        low: dict[str, int] = {}
        stack: list[str] = []
        on_stack: set[str] = set()
        result: list[list[str]] = []
        counter = 0

        def visit(node: str) -> None:
            nonlocal counter
            index[node] = low[node] = counter
            counter += 1
            stack.append(node)
            on_stack.add(node)
            for dep in sorted(self._edges.get(node, ())):
                if dep not in index:
                    visit(dep)
                    low[node] = min(low[node], low[dep])
                elif dep in on_stack:
                    low[node] = min(low[node], index[dep])
            if low[node] == index[node]:
                component = []
                while True:
                    item = stack.pop()
                    on_stack.discard(item)
                    component.append(item)
                    if item == node:
                        break
                if len(component) > 1 or node in self._edges.get(node, ()):
                    result.append(sorted(component))

        for node in sorted(self._edges):
            if node not in index:
                visit(node)
        return result

    def to_dict(self) -> dict[str, list[str]]:
        return {k: sorted(v) for k, v in sorted(self._edges.items())}
