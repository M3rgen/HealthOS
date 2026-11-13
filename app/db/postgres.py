"""Stable advisory-lock identities and safe PostgreSQL error codes.

Reserve every two-int lock namespace here, even for a single fixed key. PostgreSQL's
bigint locks occupy a separate key space. Never renumber an existing reservation:
concurrent processes and the frozen migration SQL must keep taking the same locks.
"""

import zlib
from types import MappingProxyType

from app.core import sqlstate as _sqlstate

# Compatibility exports; extraction and validation live only in core.
sqlstate = _sqlstate.extract
LOCK_NOT_AVAILABLE = _sqlstate.LOCK_NOT_AVAILABLE

LOCK_NAMESPACES = MappingProxyType(
    {
        "identity_data_type": 0x48454931,  # HEI1; shared writers, exclusive reconciler
        "identity_backfill": 0x48454932,  # HEI2; backfill and owner decisions
        "identity_deferrals": 0x48454933,  # HEI3; deferred-deletion bookkeeping
        "manual_flags": 0x48464C31,  # HFL1; measurement/flag pair
        "credentials": 0x48454331,  # HEC1; OAuth writes
        "sync_run": 0x48455331,  # HES1; per Google data type
        "notification": 0x48455731,  # HEW1; independent of data-type runs
        "garmin": 0x47524D31,  # GRM1; per kind, including normalize
    }
)
BIGINT_LOCKS = MappingProxyType({"derived_build": 72_184_518, "watchdog": 7_104_201_986})

DATA_TYPE_LOCK = LOCK_NAMESPACES["identity_data_type"]
BACKFILL_LOCK = (LOCK_NAMESPACES["identity_backfill"], 1)
DEFERRAL_LOCK = LOCK_NAMESPACES["identity_deferrals"]
FLAG_WRITE_LOCK = LOCK_NAMESPACES["manual_flags"]
CREDENTIAL_LOCK_NAMESPACE = LOCK_NAMESPACES["credentials"]
CREDENTIAL_LOCK_KEY = 0
RUN_NAMESPACE = LOCK_NAMESPACES["sync_run"]
NOTIFICATION_NAMESPACE = LOCK_NAMESPACES["notification"]
GARMIN_NAMESPACE = LOCK_NAMESPACES["garmin"]
BUILD_LOCK = BIGINT_LOCKS["derived_build"]
WATCHDOG_LOCK_ID = BIGINT_LOCKS["watchdog"]
# Local connection-quota bucket for the bigint watchdog lock, not a two-int lock namespace.
WATCHDOG_NAMESPACE = 0


def advisory_key(value: str) -> int:
    """Stable signed int32 key. A CRC collision only serializes unrelated operations."""
    return zlib.crc32(value.encode()) - 2**31
