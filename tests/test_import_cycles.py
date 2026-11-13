"""All explicit app imports, including local, relative and type-only dependencies."""

import ast
from importlib.util import resolve_name
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def import_graph(sources: dict[str, str]) -> dict[str, set[str]]:
    modules = {name.removesuffix(".__init__") for name in sources}
    graph = {name: set() for name in modules}
    for name, source in sources.items():
        module = name.removesuffix(".__init__")
        package = module if name.endswith(".__init__") else module.rpartition(".")[0]
        for node in ast.walk(ast.parse(source, filename=name)):
            targets = []
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = resolve_name("." * node.level + (node.module or ""), package)
                # `from package import submodule` depends on the concrete submodule;
                # imported attributes depend on the module exporting them instead.
                targets = [
                    target if (target := f"{base}.{alias.name}") in modules else base
                    for alias in node.names
                ]
            graph[module].update(target for target in targets if target in modules)
    return graph


def cycle(graph: dict[str, set[str]]) -> list[str]:
    visited: set[str] = set()
    active: dict[str, int] = {}
    path: list[str] = []

    def visit(module):
        if module in active:
            return [*path[active[module] :], module]
        if module in visited:
            return []
        visited.add(module)
        active[module] = len(path)
        path.append(module)
        for target in sorted(graph[module]):
            if found := visit(target):
                return found
        path.pop()
        del active[module]
        return []

    for module in sorted(graph):
        if found := visit(module):
            return found
    return []


def test_app_import_graph_has_no_cycles():
    graph = import_graph(
        {
            ".".join(path.relative_to(ROOT).with_suffix("").parts): path.read_text()
            for path in (ROOT / "app").rglob("*.py")
        }
    )
    assert not (found := cycle(graph)), "Import cycle: " + " -> ".join(found)
