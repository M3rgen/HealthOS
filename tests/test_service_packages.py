"""Service package interfaces preserve legacy callers without loading unrelated services."""

import ast
import importlib
import pkgutil
import re
import subprocess
import sys
import tomllib
import warnings
from importlib.util import resolve_name
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ROOT / "app/services"
PACKAGES = (
    "archive",
    "dashboard_views",
    "identities",
    "interpretation",
    "operations",
    "providers",
    "readers",
    "shared",
    "synchronization",
    "umay",
)
MODULES = sorted(
    (path.parent.name, path.stem)
    for package in PACKAGES
    for path in (SERVICES / package).glob("*.py")
    if path.stem != "__init__"
)
LEGACY_MODULES = MODULES
MAX_DEEP_IMPORTS = 158 + 3  # + apple_readers: dashboard_rows, dashboard_window, reading (Apple)


def service_import_targets(node, package, modules):
    if isinstance(node, ast.ImportFrom):
        base = resolve_name("." * node.level + (node.module or ""), package)
        names = [
            full if (full := f"{base}.{alias.name}") in modules else base for alias in node.names
        ]
    elif isinstance(node, ast.Import):
        names = [alias.name for alias in node.names]
    else:
        return
    for name in names:
        while name and name not in modules:
            name = name.rpartition(".")[0]
        if name:
            yield name


def service_deep_imports(services):
    # Match MIM-04: count imported names (not unique edges), excluding TYPE_CHECKING bodies.
    modules = {
        ("app.services." + ".".join(path.relative_to(services).with_suffix("").parts)).removesuffix(
            ".__init__"
        ): path
        for path in sorted(services.rglob("*.py"))
    }
    imports = []
    for module, path in modules.items():
        if module == "app.services":
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        type_only = {
            id(child)
            for node in ast.walk(tree)
            if isinstance(node, ast.If)
            and (getattr(node.test, "id", None) or getattr(node.test, "attr", None))
            == "TYPE_CHECKING"
            for statement in node.body
            for child in ast.walk(statement)
        }
        package = module if path.stem == "__init__" else module.rpartition(".")[0]
        for node in ast.walk(tree):
            if id(node) in type_only:
                continue
            for target in service_import_targets(node, package, modules):
                parts = target.split(".")
                flat = len(parts) == 3 and (services / f"{parts[2]}.py").is_file()
                if flat or (len(parts) >= 4 and module.split(".")[2] != parts[2]):
                    imports.append((str(path.relative_to(services)), node.lineno, target))
    return imports


def test_service_deep_imports_do_not_increase_and_reductions_lower_the_limit():
    imports = service_deep_imports(SERVICES)
    count = len(imports)
    assert count <= MAX_DEEP_IMPORTS, f"{count} > MAX_DEEP_IMPORTS={MAX_DEEP_IMPORTS}: {imports}"
    assert count == MAX_DEEP_IMPORTS, f"MAX_DEEP_IMPORTS sabiti {count} yap"


@pytest.mark.parametrize(
    "source, count",
    [
        ("import app.services.identities.owner as owner", 1),
        ("from app.services.identities.owner import first, second", 2),
        ("from ..identities import owner", 1),
        ("def call():\n    from ..identities.owner import first", 1),
        ("from app.services import backup_files", 1),
        ("from app.services.identities import public_symbol", 0),
        ("from app.services import identities", 0),
        ("from . import local", 0),
        ("if TYPE_CHECKING:\n    from ..identities import owner", 0),
        ("if typing.TYPE_CHECKING:\n    from ..identities import owner", 0),
        ("if TYPE_CHECKING:\n    pass\nelse:\n    from ..identities import owner", 1),
    ],
)
def test_service_deep_import_measurement_matches_the_rubric(tmp_path, source, count):
    for name in (
        "__init__",
        "archive/__init__",
        "archive/local",
        "identities/__init__",
        "identities/owner",
        "backup_files",
    ):
        path = tmp_path / f"{name}.py"
        path.parent.mkdir(exist_ok=True)
        path.write_text("")
    (tmp_path / "archive/consumer.py").write_text(source)
    assert len(service_deep_imports(tmp_path)) == count


def test_repository_uses_canonical_service_paths():
    legacy = {
        name
        for package in PACKAGES
        for name in importlib.import_module(f"app.services.{package}").__all__
    }
    dotted = re.compile(r"\bapp\.services\.(\w+)")
    imports = re.compile(r"\bfrom\s+app\.services\s+import\s+(\([^)]*\)|[^\n;\"']+)")
    violations = []
    for directory in ("app", "tests", "scripts", "deploy", "docs", "Makefile"):
        root = ROOT / directory
        paths = root.rglob("*") if root.is_dir() else [root]
        for path in sorted(paths):
            # This file deliberately exercises alias identity, reload and -m compatibility.
            if not path.is_file() or path == Path(__file__) or "__pycache__" in path.parts:
                continue
            try:
                text = path.read_text()
            except UnicodeDecodeError:
                continue
            names = [match[1] for match in dotted.finditer(text)]
            names.extend(
                match[1]
                for statement in imports.finditer(text)
                for match in re.finditer(r"(?:^|,)\s*(\w+)", statement[1].strip("()"))
            )
            violations.extend(
                (str(path.relative_to(ROOT)), name) for name in names if name in legacy
            )
    assert violations == []


def test_implementations_are_not_merged_or_left_in_the_flat_service_layer():
    # Issue #352 baseline: all 144 modules are intentional, particularly identities/chains.
    names = [name for _, name in MODULES]
    assert len(names) == len(set(names)) >= 144
    assert len(LEGACY_MODULES) >= 144
    assert {path.stem for path in SERVICES.glob("*.py")} == {
        "__init__",
        "_compat",
        "backup_files",  # Retain the stdlib-only python -m entry point.
    }
    assert {path.name for path in SERVICES.iterdir() if (path / "__init__.py").is_file()} == set(
        PACKAGES
    )


@pytest.mark.parametrize("package", PACKAGES)
def test_package_interface_explicitly_exports_its_modules(package):
    interface = importlib.import_module(f"app.services.{package}")
    assert set(interface.__all__) == {
        module.name for module in pkgutil.iter_modules(interface.__path__)
    }
    assert len(interface.__all__) == len(set(interface.__all__))
    with pytest.raises(AttributeError, match="has no attribute"):
        _ = interface.nonexistent_service


@pytest.mark.parametrize("package, name", LEGACY_MODULES)
def test_legacy_and_public_imports_are_the_same_module(package, name, monkeypatch):
    # Overrides must reach function globals as well as exported classes and exceptions.
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", DeprecationWarning)
        implementation = importlib.import_module(f"app.services.{package}.{name}")
        interface = importlib.import_module(f"app.services.{package}")
        assert getattr(interface, name) is implementation
    assert not caught
    if name == "backup_files":  # The file shim is outside the alias loader.
        legacy = importlib.import_module(f"app.services.{name}")
    else:
        with pytest.warns(
            DeprecationWarning, match=rf"app\.services\.{name} is deprecated"
        ) as caught:
            caller_line = sys._getframe().f_lineno + 1
            legacy = importlib.import_module(f"app.services.{name}")
        assert len(caught) == 1
        assert str(caught[0].message) == (
            f"app.services.{name} is deprecated; import app.services.{package}.{name}; "
            "0.2.4'te kaldırılacak"
        )
        assert caught[0].filename == __file__
        assert caught[0].lineno == caller_line
    with warnings.catch_warnings(record=True) as repeated:
        warnings.simplefilter("always", DeprecationWarning)
        assert importlib.import_module(f"app.services.{name}") is legacy
    assert not repeated
    assert legacy is implementation is getattr(interface, name)
    marker = object()
    monkeypatch.setattr(legacy, "_compatibility_probe", marker, raising=False)
    assert implementation._compatibility_probe is marker


def test_compatibility_shims_have_a_one_release_removal_deadline():
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert tuple(int(p) for p in version.split(".")) < (0, 2, 4), (
        "Remove the flat service shims for release 0.2.4"
    )
    for name in ("_compat", "backup_files"):
        tree = ast.parse((SERVICES / f"{name}.py").read_text())
        assert (
            "Deprecated since 0.2.2.1; remove in 0.2.4, after the 0.2.3 release."
            in ast.get_docstring(tree)
        )


@pytest.mark.parametrize("module", ["app.cli.main", "app.main"])
def test_entry_points_import_without_deprecation_warnings(module):
    result = subprocess.run(
        [sys.executable, "-W", "error::DeprecationWarning", "-c", f"import {module}"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stderr == ""


@pytest.mark.parametrize(
    "statement",
    [
        "import app.services.query_ranges",
        "from app.services import query_ranges",
        "importlib.import_module('app.services.query_ranges')",
    ],
)
def test_alias_warning_points_to_the_importing_line_once(statement):
    script = f"""
import importlib
import warnings
with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter('default', DeprecationWarning)
    {statement}
    {statement}
assert len(caught) == 1
assert caught[0].category is DeprecationWarning
assert caught[0].filename == '<string>'
assert caught[0].lineno == 6
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, cwd=ROOT, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("legacy_first", [True, False])
def test_alias_import_order_preserves_the_canonical_spec_and_reload(legacy_first):
    names = ["app.services.query_ranges", "app.services.shared.query_ranges"]
    if not legacy_first:
        names.reverse()
    script = f"""
import importlib
import sys
import warnings
with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter('always', DeprecationWarning)
    first = importlib.import_module({names[0]!r})
    second = importlib.import_module({names[1]!r})
assert len(caught) == 1
assert str(caught[0].message) == (
    "app.services.query_ranges is deprecated; import app.services.shared.query_ranges; "
    "0.2.4'te kaldırılacak"
)
from app.services import query_ranges
import app.services.query_ranges as dotted
assert first is second is query_ranges is dotted
assert first.__name__ == first.__spec__.name == 'app.services.shared.query_ranges'
assert importlib.reload(first) is second
assert 'sqlalchemy' not in sys.modules
assert 'app.services.providers.garmin_normalize' not in sys.modules
import app.services
before = len(sys.meta_path)
importlib.reload(app.services)
assert len(sys.meta_path) == before
try:
    importlib.import_module('app.services.nonexistent_service')
except ModuleNotFoundError as exc:
    assert exc.name == 'app.services.nonexistent_service'
else:
    raise AssertionError('unknown service was accepted')
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, cwd=ROOT, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("package", PACKAGES)
def test_importing_a_package_does_not_eagerly_import_its_modules(package):
    script = f"""
import importlib
import sys
package = importlib.import_module('app.services.{package}')
assert not any(name.startswith(package.__name__ + '.') for name in sys.modules)
assert 'sqlalchemy' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, cwd=ROOT, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("module", ["archive_query", "canonical_sleep"])
def test_interpretation_contracts_load_without_chat_or_dashboard(module):
    script = f"""
import importlib
import sys
importlib.import_module('app.services.interpretation.{module}')
assert not any(name.startswith('app.services.umay.') for name in sys.modules)
assert 'app.services.dashboard_views.dashboard' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, cwd=ROOT, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_bridge_contract_and_host_backup_remain_independent_of_database_dependencies():
    script = """
import app.services.umay.codex_chat_thread as thread
import app.services.operations.backup_files
import sys
assert thread.SOUL_PATH.is_file()
assert 'sqlalchemy' not in sys.modules
assert 'fastapi' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, cwd=ROOT, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_application_imports_use_the_new_packages_instead_of_compatibility_files():
    legacy = {name for _, name in LEGACY_MODULES}
    violations = []
    for path in (ROOT / "app").rglob("*.py"):
        if path.parent == SERVICES:
            continue
        module = ".".join(path.relative_to(ROOT).with_suffix("").parts)
        package = (
            module.removesuffix(".__init__")
            if path.stem == "__init__"
            else module.rpartition(".")[0]
        )
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                base = resolve_name("." * node.level + (node.module or ""), package)
                targets = [base, *(f"{base}.{alias.name}" for alias in node.names)]
            elif isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            else:
                continue
            violations.extend(
                (str(path.relative_to(ROOT)), target)
                for target in targets
                if target.startswith("app.services.") and target.split(".")[2] in legacy
            )
    assert violations == []


@pytest.mark.parametrize(
    "module", ["app.services.backup_files", "app.services.operations.backup_files"]
)
def test_host_backup_module_entry_points_preserve_an_existing_target(module, tmp_path):
    target = tmp_path / "health-synthetic.dump.age"
    target.write_bytes(b"existing ciphertext")
    created = subprocess.run(
        [sys.executable, "-m", module, "create", str(target)],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=30,
    )
    assert created.returncode == 0, created.stderr
    partial = Path(created.stdout.strip())
    partial.write_bytes(b"new ciphertext")
    published = subprocess.run(
        [sys.executable, "-m", module, "publish", str(partial), str(target)],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=30,
    )
    assert published.returncode == 0, published.stderr
    assert target.read_bytes() == b"existing ciphertext"
    assert Path(published.stdout.strip()).read_bytes() == b"new ciphertext"
    assert not partial.exists()
