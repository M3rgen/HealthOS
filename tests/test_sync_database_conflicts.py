"""A deadlock or serialization failure is retried, bounded, and classified as transient."""

import pytest
from sqlalchemy.exc import DBAPIError

from app.sync import errors
from app.sync.errors import (
    CONFLICT_ATTEMPTS,
    database_conflict,
    error_code,
    is_transient,
    retry_database_conflicts,
)


class _Driver(Exception):
    def __init__(self, sqlstate, attribute="sqlstate"):
        super().__init__("synthetic")
        setattr(self, attribute, sqlstate)


def failure(sqlstate):
    return DBAPIError("SELECT 1", {}, _Driver(sqlstate))


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr(errors, "CONFLICT_BACKOFF", 0)


@pytest.mark.parametrize(
    ("sqlstate", "code"),
    [("40P01", "database_deadlock"), ("40001", "database_serialization")],
)
@pytest.mark.parametrize("attribute", ["sqlstate", "pgcode"])
def test_conflicts_are_transient_and_named(sqlstate, code, attribute):
    exc = DBAPIError("SELECT 1", {}, _Driver(sqlstate, attribute))
    assert database_conflict(exc) == code
    assert error_code(exc) == code
    assert is_transient(exc) is True


@pytest.mark.parametrize("sqlstate", ["23505", "42703", None])
def test_other_database_errors_stay_non_transient(sqlstate):
    exc = failure(sqlstate)
    assert database_conflict(exc) is None
    assert is_transient(exc) is False


async def test_retry_runs_the_transaction_again_after_a_conflict():
    calls = []

    async def attempt():
        calls.append(1)
        if len(calls) < CONFLICT_ATTEMPTS:
            raise failure("40P01")
        return "stored"

    assert await retry_database_conflicts(attempt) == "stored"
    assert len(calls) == CONFLICT_ATTEMPTS


async def test_retry_is_bounded_and_raises_the_last_conflict():
    calls = []

    async def attempt():
        calls.append(1)
        raise failure("40001")

    with pytest.raises(DBAPIError):
        await retry_database_conflicts(attempt)
    assert len(calls) == CONFLICT_ATTEMPTS


async def test_other_errors_are_not_retried():
    calls = []

    async def attempt():
        calls.append(1)
        raise failure("23505")

    with pytest.raises(DBAPIError):
        await retry_database_conflicts(attempt)
    assert len(calls) == 1
