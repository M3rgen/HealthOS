"""Service subpackages follow ORDER; edges against it (BACK_EDGES) only shrink."""

import ast
from collections import Counter
from importlib.util import resolve_name
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
# Alt paket sırası: soldaki sağdakini içe aktarabilir, tersi yasak (TYPE_CHECKING hariç).
ORDER = (
    "umay",
    "synchronization",
    "dashboard_views",
    "providers",
    "interpretation",
    "readers",
    "archive",
    "identities",
    "shared",
    "operations",
)
# Sıranın tersine giden bugünkü kenarlar ve içe aktarma ifadesi sayıları. Yalnız küçülür:
# yeni geri kenar ya da sayısı artan geri kenar testi kırar; azalan sayı buraya işlenir.
BACK_EDGES: dict[tuple[str, str], int] = {}


def runtime_imports(node: ast.AST):
    if isinstance(node, ast.If) and (
        isinstance(node.test, ast.Name)
        and node.test.id == "TYPE_CHECKING"
        or isinstance(node.test, ast.Attribute)
        and isinstance(node.test.value, ast.Name)
        and node.test.value.id == "typing"
        and node.test.attr == "TYPE_CHECKING"
    ):
        # The else branch still executes at runtime.
        for child in node.orelse:
            yield from runtime_imports(child)
        return
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        yield node
    for child in ast.iter_child_nodes(node):
        yield from runtime_imports(child)


def package_edges(sources: dict[str, str]) -> Counter[tuple[str, str]]:
    modules = {name.removesuffix(".__init__") for name in sources}
    packages = {
        name.split(".")[2]
        for name in sources
        if name.startswith("app.services.") and len(name.split(".")) >= 4
    }
    edges: Counter[tuple[str, str]] = Counter()
    for name, source in sources.items():
        parts = name.split(".")
        if len(parts) < 4 or parts[:2] != ["app", "services"]:
            continue
        package = (
            name.removesuffix(".__init__") if parts[-1] == "__init__" else name.rpartition(".")[0]
        )
        for node in runtime_imports(ast.parse(source, filename=name)):
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            else:
                base = resolve_name("." * node.level + (node.module or ""), package)
                targets = [
                    target if (target := f"{base}.{alias.name}") in modules else base
                    for alias in node.names
                ]
            # Count import statements, not the number of imported symbols.
            dependencies = set()
            for target in targets:
                target_parts = target.split(".")
                if (
                    len(target_parts) >= 3
                    and target_parts[:2] == ["app", "services"]
                    and target_parts[2] in packages
                    and parts[2] != target_parts[2]
                ):
                    dependencies.add((parts[2], target_parts[2]))
            edges.update(dependencies)
    return edges


def measured_edges():
    return package_edges(
        {
            ".".join(path.relative_to(ROOT).with_suffix("").parts): path.read_text()
            for path in (ROOT / "app" / "services").rglob("*.py")
        }
    )


def back_edges(edges):
    return {
        (source, target): count
        for (source, target), count in edges.items()
        if ORDER.index(source) > ORDER.index(target)
    }


def test_every_service_package_has_a_place_in_the_order():
    packages = {
        path.name
        for path in (ROOT / "app" / "services").iterdir()
        if (path / "__init__.py").is_file()
    }
    assert not packages - set(ORDER), f"ORDER'a yerleştirin: {sorted(packages - set(ORDER))}"


def test_service_package_edges_follow_the_order_and_back_edges_only_shrink():
    back = back_edges(measured_edges())
    grown = {
        edge: (BACK_EDGES.get(edge, 0), count)
        for edge, count in back.items()
        if count > BACK_EDGES.get(edge, 0)
    }
    assert not grown, "\n".join(
        f"sıraya aykırı alt paket bağımlılığı: {source} -> {target} ({before} -> {after})"
        for (source, target), (before, after) in sorted(grown.items())
    )
    shrunk = {
        edge: (count, back.get(edge, 0))
        for edge, count in BACK_EDGES.items()
        if back.get(edge, 0) < count
    }
    assert not shrunk, "BACK_EDGES'i küçültün: " + ", ".join(
        f"{source} -> {target}: {before} -> {after}"
        for (source, target), (before, after) in sorted(shrunk.items())
    )


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (source, target)
        for source in ORDER
        for target in ORDER
        if ORDER.index(source) > ORDER.index(target)
    ],
)
def test_a_planted_upward_import_is_a_back_edge(source, target):
    edges = package_edges(
        {
            f"app.services.{source}.planted": (
                f"def call():\n    import app.services.{target}.module"
            ),
            f"app.services.{target}.module": "",
        }
    )
    assert back_edges(edges) == {(source, target): 1}


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import app.services.interpretation.archive_query", True),
        ("from app.services.interpretation.archive_query import query", True),
        ("from app.services.interpretation.archive_query import query, other", True),
        ("from app.services import interpretation", True),
        ("from ..interpretation import archive_query", True),
        ("from .. import interpretation", True),
        ("def calculate():\n    import app.services.interpretation.archive_query", True),
        ("if TYPE_CHECKING:\n    import app.services.interpretation.archive_query", False),
        ("if typing.TYPE_CHECKING:\n    import app.services.interpretation.archive_query", False),
        (
            "if TYPE_CHECKING:\n    from ..archive import query\n"
            "else:\n    from ..interpretation import archive_query",
            True,
        ),
        ("from . import other", False),
        ("import app.services.shared.other", False),
        ("from app.core import config", False),
        ("import app.services.standalone_fixture", False),
    ],
)
def test_package_edges_resolve_runtime_imports(source, expected):
    sources = {
        "app.services.shared.finite_math": source,
        "app.services.shared.other": "",
        "app.services.interpretation.__init__": "",
        "app.services.interpretation.archive_query": "",
        "app.services.archive.__init__": "",
        "app.services.standalone_fixture": "",
    }
    assert package_edges(sources) == ({("shared", "interpretation"): 1} if expected else {})


def test_package_edges_resolve_package_initializers_and_nested_relative_imports():
    assert package_edges(
        {
            "app.services.shared.__init__": "from ..interpretation import archive_query",
            "app.services.interpretation.archive_query": "from ..shared import other",
            "app.services.shared.nested.consumer": "from ...interpretation import archive_query",
            "app.services.shared.other": "",
            "app.services.__init__": "import app.services.interpretation.archive_query",
        }
    ) == {("shared", "interpretation"): 2, ("interpretation", "shared"): 1}
