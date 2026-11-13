"""Architecture guards and import checks for the parser/service/startup boundaries."""

import ast
import subprocess
import sys
from importlib.util import resolve_name
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def imports(path):
    module = ".".join(path.relative_to(ROOT).with_suffix("").parts)
    package = (
        module.removesuffix(".__init__") if path.stem == "__init__" else module.rpartition(".")[0]
    )
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            target = resolve_name("." * node.level + (node.module or ""), package)
            yield target
            yield from (f"{target}.{alias.name}" for alias in node.names)


@pytest.mark.parametrize(
    "directory, forbidden", [("services", "fastapi"), ("parsers", "app.models")]
)
def test_layers_do_not_import_forbidden_dependencies(directory, forbidden):
    violations = [
        (str(path.relative_to(ROOT)), target)
        for path in (ROOT / "app" / directory).rglob("*.py")
        for target in imports(path)
        if target == forbidden or target.startswith(forbidden + ".")
    ]
    assert violations == []


def test_no_engine_is_constructed_at_module_scope():
    factories = {
        "create_engine",
        "create_async_engine",
        "create_shared_engine",
        "initialize_sessions",
    }
    violations = []

    class ModuleCalls(ast.NodeVisitor):
        def visit_FunctionDef(self, node):
            # Defaults and decorators execute at import too; function bodies do not.
            for child in [*node.decorator_list, *node.args.defaults, *node.args.kw_defaults]:
                if child is not None:
                    self.visit(child)

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node):
            name = (
                node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
            )
            if name in factories:
                violations.append((str(path.relative_to(ROOT)), node.lineno))
            self.generic_visit(node)

    for path in (ROOT / "app").rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                factories.update(
                    alias.asname for alias in node.names if alias.name in factories and alias.asname
                )
        ModuleCalls().visit(tree)
    assert violations == []


def test_entry_point_imports_never_create_an_engine_even_indirectly():
    script = """
from importlib import import_module
from unittest.mock import patch
failure = AssertionError('import-time engine')
with patch('sqlalchemy.create_engine', side_effect=failure), \\
     patch('sqlalchemy.ext.asyncio.create_async_engine', side_effect=failure):
    for name in ('app.db.session', 'app.services.umay.codex_health', 'app.cli.main', 'app.main'):
        import_module(name)
"""
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_services_never_end_a_transaction_on_a_supplied_session():
    violations = []
    for path in (ROOT / "app/services").rglob("*.py"):
        for function in ast.walk(ast.parse(path.read_text())):
            if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if "session" not in {
                arg.arg for arg in [*function.args.args, *function.args.kwonlyargs]
            }:
                continue
            for node in ast.walk(function):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "session"
                    and node.func.attr in {"commit", "rollback"}
                ):
                    violations.append((str(path.relative_to(ROOT)), function.name, node.func.attr))
    assert violations == []


def test_parser_returns_only_a_record_kind_and_values():
    from app.google_health.data_types import REGISTRY
    from app.parsers.normalize import normalize

    record = {
        "steps": {
            "interval": {"startTime": "2026-09-01T00:00:00Z", "endTime": "2026-09-01T01:00:00Z"},
            "count": 123,
        }
    }
    parsed = normalize(REGISTRY["steps"], record)
    assert parsed is not None
    assert parsed.kind == "Steps" and parsed.values["count"] == 123
    assert not hasattr(parsed, "model")


@pytest.mark.parametrize("operation", ["receive_json", "send_json", "close"])
async def test_api_translates_browser_disconnects_into_service_errors(operation):
    from unittest.mock import AsyncMock, Mock

    from fastapi import WebSocketDisconnect

    from app.api.chat_socket import BrowserSocket
    from app.services.umay.chat_socket import ChatDisconnected

    websocket = Mock()
    setattr(websocket, operation, AsyncMock(side_effect=WebSocketDisconnect()))
    browser = BrowserSocket(websocket)
    with pytest.raises(ChatDisconnected):
        if operation == "send_json":
            await browser.send_json({"type": "done"})
        elif operation == "close":
            await browser.close()
        else:
            await browser.receive_json()


def test_service_json_preserves_the_existing_wire_values():
    from datetime import UTC, date, datetime
    from decimal import Decimal
    from uuid import UUID

    from fastapi.encoders import jsonable_encoder

    from app.core.json_values import json_values

    value = {
        "today": date(2026, 9, 1),
        "start": datetime(2026, 9, 1, 1, 2, 3, 400, tzinfo=UTC),
        "values": [Decimal("0"), Decimal("70.5"), Decimal("1E2"), None],
        "id": UUID(int=1),
    }
    assert json_values(value) == jsonable_encoder(value)
