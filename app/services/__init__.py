"""Application services, grouped by responsibility.

Import modules through their subpackage, e.g. ``app.services.shared.query_ranges``. The flat
``app.services.<module>`` aliases were removed in 0.2.4.
"""

from typing import TYPE_CHECKING

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
