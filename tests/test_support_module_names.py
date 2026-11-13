"""Helpers on PYTHONPATH must not shadow other importable modules or packages."""

import sys
from importlib.machinery import PathFinder
from importlib.util import find_spec
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def importable_names(directory: Path) -> set[str]:
    candidates = {
        path.stem if path.is_file() and path.suffix == ".py" else path.name
        for path in directory.iterdir()
        if path.name != "__pycache__"
    }
    # PathFinder also recognizes namespace packages without __init__.py, without
    # importing modules or consulting an already populated sys.modules cache.
    return {
        name
        for name in candidates
        if name.isidentifier() and PathFinder.find_spec(name, [str(directory)]) is not None
    }


def test_support_modules_do_not_shadow_repository_imports(monkeypatch):
    support = ROOT / "tests" / "support"
    # Resolve only against the repository, the interpreter and its installed packages.
    # Other tests in the same xdist worker may leave temporary directories on sys.path
    # (or change the working directory), and a directory there that happens to share a
    # helper name would otherwise look like a shadowed namespace package.
    trusted = [ROOT, *(Path(prefix).resolve() for prefix in {sys.prefix, sys.base_prefix})]
    monkeypatch.setattr(
        sys,
        "path",
        [
            entry
            for entry in sys.path
            if entry
            and Path(entry).resolve() != support
            and any(Path(entry).resolve().is_relative_to(root) for root in trusted)
        ],
    )
    collisions = {}
    for name in sorted(importable_names(support)):
        # Resolve afresh even if a helper was already imported via PYTHONPATH.
        monkeypatch.delitem(sys.modules, name, raising=False)
        spec = find_spec(name)
        if spec is not None:
            collisions[name] = spec.origin or list(spec.submodule_search_locations or [])
    assert not collisions, f"tests/support shadows other imports: {collisions}"
