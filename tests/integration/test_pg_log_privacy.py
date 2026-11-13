"""Real PostgreSQL errors that quote a health record or source metadata stay out of the logs.

A trigger rejects the write the way a constraint would, with the private value in the error's
message and DETAIL (and SQLAlchemy adds the bound parameters): the backfill meets a data
error (SQLSTATE 22023; asyncpg's dialect raises it as ``DBAPIError``), the identity reconciler
an ``IntegrityError``. Only the class chain, SQLSTATE and code locations may leave
(tests/test_log_privacy.py has the SQLite and library cases).
"""

from __future__ import annotations

import logging

import pytest
from identity_records import STEPS, T0, fitbit, steps, store
from sqlalchemy import select, text
from support.log_privacy import MARKER, assert_clean, backfill_job, backfill_service, capture_logs

from app.models.identity_tables import IdentityReconcileState
from app.models.state import JobStatus, RawHealthData
from app.services.identities import source_chain_repository
from app.services.identities.identity_reconciler import IdentityReconciler

pytestmark = pytest.mark.integration


@pytest.fixture
def emitted(pg_sessions):
    # After the migration: its fileConfig() disabled the application's loggers
    # (tests/conftest.py restores them after the test).
    for item in list(logging.Logger.manager.loggerDict.values()):
        if isinstance(item, logging.Logger):
            item.disabled = False
    return capture_logs()


async def reject(sessions, table: str, column: str, sqlstate: str) -> None:
    async with sessions.begin() as session:
        await session.execute(
            text(
                f"CREATE FUNCTION reject_{table}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
                f"RAISE EXCEPTION 'rejected %', NEW.{column}::text "
                f"USING DETAIL = 'row ' || NEW.{column}::text, ERRCODE = '{sqlstate}'; END $$"
            )
        )
        await session.execute(
            text(
                f"CREATE TRIGGER reject_{table} BEFORE INSERT OR UPDATE ON {table} "
                f"FOR EACH ROW EXECUTE FUNCTION reject_{table}()"
            )
        )


async def test_backfill_data_error_from_postgres_stays_out_of_the_logs(pg_sessions, emitted):
    await reject(pg_sessions, "raw_health_data", "raw_json", "22023")

    assert await backfill_service(pg_sessions).run(["steps"]) == 0

    job = await backfill_job(pg_sessions)
    assert (job.status, job.retry_count, job.last_error) == (JobStatus.failed, 1, "DBAPIError")
    async with pg_sessions() as session:
        assert (await session.scalars(select(RawHealthData))).all() == []
    assert_clean(emitted)
    (failed,) = emitted.events("BACKFILL_WINDOW_FAILED")
    assert (failed["error_type"], failed["sqlstate"]) == ("DBAPIError", "22023")
    assert failed["error_chain"].startswith("DBAPIError < ")
    assert "InvalidParameterValueError" in failed["error_chain"]
    assert "app.sync.backfill:_run_owned_job:" in failed["error_frames"]


async def test_identity_integrity_error_from_postgres_stays_out_of_logs_and_state(
    pg_sessions, emitted
):
    await store(pg_sessions, STEPS, steps(40, start=T0, source=fitbit(MARKER)))
    await reject(pg_sessions, "raw_source_sightings", "data_source", "23514")

    summary = await IdentityReconciler(pg_sessions).run()

    assert summary is not None
    error = summary["steps"]["error"]
    assert error.startswith("IntegrityError < ") and error.endswith("(SQLSTATE 23514)")
    assert MARKER not in error
    async with pg_sessions() as session:
        state = await session.get(IdentityReconcileState, "steps")
    assert state is not None and state.last_error == error
    assert_clean(emitted)
    (failed,) = emitted.events("IDENTITY_RECONCILE_DATA_TYPE_FAILED")
    assert (failed["error_type"], failed["sqlstate"]) == ("IntegrityError", "23514")
    assert f"{source_chain_repository.__name__}:write_spans:" in failed["error_frames"]
    (result,) = emitted.events("IDENTITY_RECONCILE_DATA_TYPE")
    assert result["error"] == error
