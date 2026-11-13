"""Application services, grouped by responsibility.

Use the subpackage interfaces for new imports. Flat module paths are compatibility
aliases that emit DeprecationWarning with the replacement path on first import. They
have been deprecated since 0.2.2.1; remove in 0.2.4, after the 0.2.3 release.
"""

from typing import TYPE_CHECKING

from ._compat import install as _install

if TYPE_CHECKING:
    from . import (
        archive,
        dashboard_views,
        identities,
        interpretation,
        operations,
        providers,
        readers,
        shared,
        synchronization,
        umay,
    )

__all__ = [
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
]

_install(__name__, __all__)
