"""Layer imports point downwards, including deferred and type-only imports."""

import ast
import re
from importlib.util import resolve_name
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MAY_IMPORT = {
    "api": {"services", "schemas", "core", "google_health", "garmin", "db", "runtime"},
    "cli": {"services", "runtime", "db", "core"},
    "services": {"parsers", "models", "schemas", "db", "google_health", "garmin", "apple", "core"},
    "sync": {"services", "models", "db", "google_health", "core"},
    "parsers": {"core"},
    "google_health": {"core"},
    "garmin": {"core"},
    "apple": {"core", "google_health"},  # GitHub-only Apple extension (export and HAE parsers)
    "schemas": set(),
    "models": {"db", "core"},
    "db": {"core"},
    "core": set(),
}
APP_MODULES = {
    path.stem
    for path in (ROOT / "app").iterdir()
    if (path.is_dir() and any(path.rglob("*.py")))
    or (path.suffix == ".py" and path.stem != "__init__")
}


def test_architecture_table_matches_layer_allowlist():
    document = (ROOT / "docs" / "ARCHITECTURE.md").read_text()
    header = "| Layer | Package | Responsibility | May import |"
    assert document.count(header) == 1, "Expected exactly one architecture layer table"
    table = document.split(header, 1)[1].strip().split("\n\n", 1)[0]
    documented = {}
    for row in table.splitlines()[1:]:
        _, _, packages, _, cell, _ = row.split("|")
        layers = re.findall(r"`app/([\w.]+)`", packages)
        assert layers, f"Missing app package in layer row: {row}"
        cell = cell.strip()
        if cell == "everything below":
            allowed = set(MAY_IMPORT)
        elif cell.startswith("no other `app` package"):
            allowed = set()
        else:
            # Negative examples describe forbidden imports, not permissions.
            allowed = set(re.findall(r"`(\w+)`", cell.split("; no ", 1)[0]))
        for layer in layers:
            assert layer not in documented, f"Duplicate architecture layer: {layer}"
            documented[layer] = allowed
    expected = MAY_IMPORT | {"main.py": set(MAY_IMPORT), "runtime.py": set(MAY_IMPORT)}
    assert documented.keys() == expected.keys(), (
        f"Architecture layers: documented={sorted(documented)}, allowlist={sorted(expected)}"
    )
    for layer, allowed in expected.items():
        assert documented[layer] == allowed, (
            f"{layer}: documented={sorted(documented[layer])}, allowlist={sorted(allowed)}"
        )


def imports(path):
    relative = path.relative_to(ROOT)
    package = ".".join(relative.parent.parts)
    for node in ast.walk(ast.parse(path.read_text(), filename=str(relative))):
        if isinstance(node, ast.Import):
            targets = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            module = resolve_name("." * node.level + (node.module or ""), package)
            targets = [module, *(f"{module}.{alias.name}" for alias in node.names)]
        else:
            continue
        yield from ((target, node.lineno) for target in targets)


@pytest.mark.parametrize("layer", MAY_IMPORT)
def test_layer_imports_follow_architecture_allowlist(layer):
    allowed = (MAY_IMPORT[layer] | {layer}) & APP_MODULES
    violations = [
        f"{path.relative_to(ROOT)}:{line}: {target}"
        for path in sorted((ROOT / "app" / layer).rglob("*.py"))
        for target, line in imports(path)
        if target.startswith("app.") and target.split(".")[1] not in allowed
    ]
    assert not violations, f"{layer} imports outside May import: " + ", ".join(violations)


@pytest.mark.parametrize(
    "layer, forbidden",
    [
        ("api", "app.models"),
        ("api", "app.sync"),
        ("cli", "app.models"),
        ("cli", "app.sync"),
        ("cli", "app.api"),
        ("core", "fastapi"),
        *[
            ("core", f"app.{path.stem}")
            for path in sorted((ROOT / "app").iterdir())
            if (path.is_dir() or path.suffix == ".py")
            and path.stem not in {"core", "__init__", "__pycache__"}
        ],
        ("google_health", "app.db"),
        ("google_health", "app.models"),
        ("garmin", "app.db"),
        ("garmin", "app.models"),
        ("garmin", "app.services"),
        ("parsers", "app.db"),
        ("services", "app.api"),
        ("services", "app.sync"),
        ("sync", "app.api"),
        ("sync", "app.cli"),
        *[
            (layer, f"app.{upper}")
            for layer in ("models", "google_health")
            for upper in ("services", "sync", "api")
        ],
    ],
)
def test_layer_import_boundary(layer, forbidden):
    violations = []
    for path in sorted((ROOT / "app" / layer).rglob("*.py")):
        relative = path.relative_to(ROOT)
        for target, line in imports(path):
            if target == forbidden or target.startswith(forbidden + "."):
                violations.append(f"{relative}:{line}")
    assert not violations, f"{layer} must not import {forbidden}: " + ", ".join(violations)
