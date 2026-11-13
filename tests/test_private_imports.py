"""Keep shared implementation details behind public module interfaces."""

import ast
from importlib.util import resolve_name
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULES = {
    ".".join(path.relative_to(ROOT).with_suffix("").parts).removesuffix(".__init__")
    for path in (ROOT / "app").rglob("*.py")
}
# Exact (importing module, defining module, symbol) -> reason. No exceptions remain.
ALLOWED: dict[tuple[str, str, str], str] = {}
LIMIT = 10


def private_imports(module: str, source: str) -> list[tuple[str, str, str]]:
    current = module.removesuffix(".__init__")
    package = current if module.endswith(".__init__") else current.rpartition(".")[0]
    imports = []
    tree = ast.parse(source, filename=module)
    bindings = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            bindings.update((alias.asname or alias.name, alias.name) for alias in node.names)
        if not isinstance(node, ast.ImportFrom):
            continue
        target = resolve_name("." * node.level + (node.module or ""), package)
        bindings.update(
            (alias.asname or alias.name, imported)
            for alias in node.names
            if (imported := f"{target}.{alias.name}") in MODULES
        )
        if target != current:
            imports.extend(
                (current, target, alias.name) for alias in node.names if alias.name.startswith("_")
            )
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or not node.attr.startswith("_"):
            continue
        name = ast.unparse(node.value)
        if name not in {"self", "cls"} and (target := bindings.get(name)) and target != current:
            imports.append((current, target, node.attr))
    return imports


def test_app_private_imports_stay_within_the_documented_limit():
    imports = [
        item
        for path in (ROOT / "app").rglob("*.py")
        for item in private_imports(
            ".".join(path.relative_to(ROOT).with_suffix("").parts), path.read_text()
        )
    ]
    assert len(imports) <= LIMIT, f"{len(imports)} private imports exceed {LIMIT}: {imports}"
    assert all(reason.strip() for reason in ALLOWED.values())
    assert not (undocumented := set(imports) - ALLOWED.keys()), undocumented
    assert not (stale := ALLOWED.keys() - set(imports)), f"Stale exceptions: {stale}"


@pytest.mark.parametrize(
    "source",
    [
        "from app.example.owner import _shared as public",
        "def call():\n    from .owner import _shared",
        "if TYPE_CHECKING:\n    from .owner import _shared",
        "from .owner import public, _shared",
    ],
)
def test_private_import_detection_includes_aliases_local_relative_and_type_only_imports(source):
    assert private_imports("app.example.consumer", source) == [
        ("app.example.consumer", "app.example.owner", "_shared")
    ]


def test_public_symbols_and_private_local_aliases_are_allowed():
    assert private_imports("app.example.consumer", "from .owner import shared as _local") == []


@pytest.mark.parametrize(
    "source",
    [
        "import app.core.sqlstate as driver; driver._SQLSTATE",
        "import app.core.sqlstate; app.core.sqlstate._SQLSTATE",
        "from app.core import sqlstate as driver; driver._SQLSTATE",
        "from app.core import sqlstate; sqlstate._SQLSTATE",
        "def call():\n    from ..core import sqlstate as driver\n    driver._SQLSTATE",
        "if TYPE_CHECKING:\n    from app.core import sqlstate\n    sqlstate._SQLSTATE",
    ],
)
def test_private_module_attributes_are_detected(source):
    assert private_imports("app.example.consumer", source) == [
        ("app.example.consumer", "app.core.sqlstate", "_SQLSTATE")
    ]


def test_own_module_public_attributes_and_instance_details_are_allowed():
    source = (
        "from app.db import postgres as own; own._local; own.public; self._x; cls._x; "
        "from app.models.state import RawHealthData; RawHealthData.__table__"
    )
    assert private_imports("app.db.postgres", source) == []
