"""Lock reservations cannot collide or drift from other running processes."""

from types import SimpleNamespace

import pytest
from sqlalchemy.exc import DBAPIError

from app.core import sqlstate as core_sqlstate
from app.db import postgres


def test_postgres_reexports_the_core_sqlstate_primitives():
    assert postgres.sqlstate is core_sqlstate.sqlstate
    assert core_sqlstate.extract is core_sqlstate.sqlstate
    assert postgres.LOCK_NOT_AVAILABLE == core_sqlstate.LOCK_NOT_AVAILABLE == "55P03"


def test_sqlstate_constants_keep_the_postgres_driver_codes():
    assert core_sqlstate.DEADLOCK_DETECTED == "40P01"
    assert core_sqlstate.SERIALIZATION_FAILURE == "40001"
    assert core_sqlstate.LOCK_NOT_AVAILABLE == "55P03"


def test_advisory_namespaces_and_bigint_locks_are_unique_and_in_range():
    # PostgreSQL keeps bigint and (int32, int32) locks in separate key spaces.
    namespaces = list(postgres.LOCK_NAMESPACES.values())
    assert len(namespaces) == len(set(namespaces))
    assert all(-(2**31) <= value < 2**31 for value in namespaces)
    bigints = list(postgres.BIGINT_LOCKS.values())
    assert len(bigints) == len(set(bigints))
    assert all(-(2**63) <= value < 2**63 for value in bigints)


def test_reservations_keep_existing_process_and_trigger_identities():
    assert dict(postgres.LOCK_NAMESPACES) == {
        "identity_data_type": 0x48454931,
        "identity_backfill": 0x48454932,
        "identity_deferrals": 0x48454933,
        "manual_flags": 0x48464C31,
        "credentials": 0x48454331,
        "sync_run": 0x48455331,
        "notification": 0x48455731,
        "garmin": 0x47524D31,
    }
    assert dict(postgres.BIGINT_LOCKS) == {
        "derived_build": 72184518,
        "watchdog": 7104201986,
        "heart_projection": 72184536,
    }
    assert postgres.BACKFILL_LOCK == (0x48454932, 1)
    assert postgres.CREDENTIAL_LOCK_KEY == 0


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", -2147483648),
        ("steps", -1272837518),
        ("heart-rate", -2018325127),
        ("ölçüm", -1156887047),
    ],
)
def test_dynamic_keys_remain_stable_signed_int32(value, expected):
    assert postgres.advisory_key(value) == expected


@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("attribute", ["sqlstate", "pgcode"])
@pytest.mark.parametrize("code", ["55P03", "40P01", "40001", "23505"])
def test_sqlstate_accepts_both_driver_attributes_and_sqlalchemy_wrappers(wrapped, attribute, code):
    driver = SimpleNamespace(**{attribute: code})
    error = DBAPIError("synthetic", {}, driver) if wrapped else driver
    assert postgres.sqlstate(error) == code


@pytest.mark.parametrize("attribute", ["sqlstate", "pgcode"])
@pytest.mark.parametrize("code", [None, "", "2350", "235050", "40p01", "23505\n", 23505, []])
def test_sqlstate_never_returns_untrusted_values(attribute, code):
    driver = SimpleNamespace(**{attribute: code})
    assert postgres.sqlstate(DBAPIError("synthetic", {}, driver)) is None


def test_sqlstate_precedence_fallback_and_no_message_parsing():
    assert postgres.sqlstate(SimpleNamespace(sqlstate="55P03", pgcode="40P01")) == "55P03"
    assert postgres.sqlstate(SimpleNamespace(sqlstate=None, pgcode="40P01")) == "40P01"
    assert postgres.sqlstate(RuntimeError("55P03")) is None
    assert postgres.sqlstate(DBAPIError("synthetic", {}, None)) is None
