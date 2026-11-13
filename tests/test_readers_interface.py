"""Reader contracts stay lazy and preserve module and symbol identities."""

import ast
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("module_first", [False, True])
@pytest.mark.parametrize(
    ("module", "names"),
    [
        ("dashboard_queries", ("CalendarRead", "DashboardQuery")),
        ("dashboard_rows", ("day_rows",)),
        ("dashboard_sources", ("DashboardSources",)),
        ("dashboard_window", ("DashboardWindow", "checked_dates", "daily_window")),
        ("reading", ("ArchiveReaders", "Archives", "DailyRead", "HistoryMetric")),
    ],
)
def test_reader_contracts_load_lazily_and_preserve_identity(module, names, module_first):
    script = f"""
import importlib
import sys
from types import ModuleType

interface = importlib.import_module('app.services.readers')
assert not any(name.startswith(interface.__name__ + '.') for name in sys.modules)
if {module_first!r}:
    importlib.import_module('app.services.readers.{module}')
for name in {names!r}:
    value = getattr(interface, name)
    implementation = importlib.import_module('app.services.readers.{module}')
    assert value is getattr(implementation, name)
    assert vars(interface)[name] is value
    assert getattr(interface, name) is value

# A module name always gives the module, even when it also names a function.
leaf = getattr(interface, {module!r})
assert isinstance(leaf, ModuleType)
assert leaf is implementation
loaded = {{name for name in sys.modules if name.startswith('app.services.readers.')}}
assert 'app.services.readers.{module}' in loaded
assert not any(
    name.startswith('app.services.' + package + '.')
    for name in sys.modules
    for package in ('dashboard_views', 'interpretation', 'providers', 'synchronization', 'umay')
)
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, cwd=ROOT, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stderr == ""


def test_contract_names_are_exactly_the_names_other_packages_import_from_the_interface():
    from app.services import readers

    modules = set(readers.__all__)
    imported = {
        alias.name
        for path in (ROOT / "app" / "services").rglob("*.py")
        if path.parent.name != "readers"
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.ImportFrom) and node.module == "app.services.readers"
        for alias in node.names
        if alias.name not in modules
    }
    assert set(readers._CONTRACT) == imported
