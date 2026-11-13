"""Flat service aliases. Deprecated since 0.2.2.1; remove in 0.2.4, after the 0.2.3 release.

Keep aliases out of the source tree so implementations remain actual file moves.
The package interfaces are the only module registry; no implementation is loaded
until an old import path is requested.
"""

import importlib
import sys
import warnings
from collections.abc import Sequence
from importlib.abc import Loader, MetaPathFinder
from importlib.machinery import ModuleSpec
from importlib.util import find_spec, spec_from_loader
from os.path import dirname
from types import ModuleType

_IMPORTLIB_SPEC = find_spec("importlib")
_IMPORTLIB_PREFIXES = (
    (dirname(_IMPORTLIB_SPEC.origin),)
    if _IMPORTLIB_SPEC and _IMPORTLIB_SPEC.has_location and _IMPORTLIB_SPEC.origin
    else ()
)


class AliasLoader(Loader):
    def __init__(self, target: str):
        self.target = target

    def create_module(self, spec: ModuleSpec) -> None:
        return None

    def exec_module(self, module: ModuleType) -> None:
        warnings.warn(
            f"{module.__name__} is deprecated; import {self.target}; 0.2.4'te kaldırılacak",
            DeprecationWarning,
            stacklevel=2,
            # Python 3.12 skips frozen import frames itself; also skip import_module's
            # public wrapper so both import statements and dynamic imports name the caller.
            skip_file_prefixes=_IMPORTLIB_PREFIXES,
        )
        # Replace the temporary module instead of changing the target's name/spec.
        # Both paths must share globals, exceptions and monkeypatches.
        sys.modules[module.__name__] = importlib.import_module(self.target)


class AliasFinder(MetaPathFinder):
    def __init__(self, namespace: str, packages: Sequence[str]):
        self.namespace = namespace
        self.packages = tuple(packages)

    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None,
        target: ModuleType | None = None,
    ) -> ModuleSpec | None:
        parent, _, name = fullname.rpartition(".")
        if parent != self.namespace or name.startswith("_") or name in self.packages:
            return None
        for package in self.packages:
            interface = importlib.import_module(f"{self.namespace}.{package}")
            if name in interface.__all__:
                return spec_from_loader(fullname, AliasLoader(f"{interface.__name__}.{name}"))
        return None


def install(namespace: str, packages: Sequence[str]) -> None:
    """Install once, after normal file loading (the backup -m shim stays a file)."""
    if not any(isinstance(finder, AliasFinder) for finder in sys.meta_path):
        sys.meta_path.append(AliasFinder(namespace, packages))
