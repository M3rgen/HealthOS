"""Backend selection stays centralized; runtime SQL never installs the SQLite test adapter."""

import ast
import importlib
from pathlib import Path

from support.sqlite_operations import OPERATIONS

ROOT = Path(__file__).resolve().parents[1]


def test_dialect_selection_stays_in_its_database_module():
    branches = {
        str(path.relative_to(ROOT))
        for path in (ROOT / "app").rglob("*.py")
        if any(
            isinstance(node, ast.Attribute)
            and node.attr == "name"
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "dialect"
            for node in ast.walk(ast.parse(path.read_text()))
        )
    }
    assert branches == {"app/db/dialect.py"}


def test_sqlite_runtime_implementations_live_only_in_test_support():
    references = {
        str(path.relative_to(ROOT))
        for path in (ROOT / "app").rglob("*.py")
        if "sqlite" in path.read_text().lower()
    }
    assert references == {"app/db/dialect.py"}
    violations = []
    for path in (ROOT / "app").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                if (node.module or "").startswith(
                    ("sqlalchemy.dialects.sqlite", "support", "tests")
                ):
                    violations.append((path, node.lineno))
            elif isinstance(node, ast.Import):
                if any(
                    alias.name.startswith(("sqlalchemy.dialects.sqlite", "support", "tests"))
                    for alias in node.names
                ):
                    violations.append((path, node.lineno))
    assert violations == []


def test_sqlite_alternatives_target_real_dispatched_operations():
    dispatched = {
        ".".join((*path.relative_to(ROOT).with_suffix("").parts, node.name))
        for path in (ROOT / "app").rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and any(
            isinstance(d, ast.Name) and d.id == "database_operation" for d in node.decorator_list
        )
    }
    assert dispatched == OPERATIONS.keys()
    for qualified in OPERATIONS:
        module, _, name = qualified.rpartition(".")
        operation = getattr(importlib.import_module(module), name)
        assert hasattr(operation, "__wrapped__"), qualified
        assert operation.__wrapped__.__module__ == module
        assert operation.__wrapped__.__name__ == name


def test_cli_never_constructs_or_executes_a_database_query():
    violations = []
    for path in (ROOT / "app/cli").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module == "sqlalchemy":
                violations.append((path, node.lineno))
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if (
                    isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "session"
                    and node.func.attr in {"execute", "scalar", "scalars", "stream", "add", "get"}
                ):
                    violations.append((path, node.lineno))
    assert violations == []


def test_dashboard_input_models_delegate_domain_rules_to_services():
    tree = ast.parse((ROOT / "app/api/dashboard.py").read_text())
    validators = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name in {"valid_workout", "valid_value"}
    }
    assert validators.keys() == {"valid_workout", "valid_value"}
    for name, function in validators.items():
        expected = "validate_workout" if name == "valid_workout" else "validate_measurement"
        assert any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == expected
            for node in ast.walk(function)
        )
        assert not any(isinstance(node, ast.Compare | ast.BinOp) for node in ast.walk(function))
