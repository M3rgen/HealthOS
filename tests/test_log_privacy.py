"""Health values and source metadata never reach the logs through exception text (CWE-532).

A failed write's database error quotes what it was given: SQLAlchemy appends the bound
parameters (a whole ``raw_json`` record, a ``dataSource``), the database adds its message and
DETAIL. The failure paths log value-free diagnostics (class chain, SQLSTATE, code locations)
and every handler drops ``exc_info``. ``MARKER`` (tests/support/log_privacy.py) stands for a
private value.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import sys

import pytest
from identity_records import STEPS, T0, fitbit, steps, store
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from support.log_privacy import MARKER, assert_clean, backfill_job, backfill_service, capture_logs

from app.core.logging import configure_logging, error_diagnostics, error_frames, error_text
from app.models.identity_tables import IdentityReconcileState
from app.models.state import JobStatus
from app.runtime import Runtime
from app.services.identities import identity_reconciler
from app.services.identities.identity_backfill import (
    LockUnavailable,
    failure_summary,
    identity_backfill,
)
from app.services.identities.identity_reconcile_window import LOCK_ERROR, lock_failed
from app.services.identities.identity_reconciler import IdentityReconciler
from app.services.providers import ingestion


@pytest.fixture
def emitted():
    return capture_logs()


async def reject(sessions, table: str) -> None:
    """A trigger that makes every write to ``table`` fail, as a real constraint would."""
    async with sessions.begin() as session:
        for when in ("INSERT", "UPDATE"):
            await session.execute(
                text(
                    f"CREATE TRIGGER reject_{table}_{when.lower()} BEFORE {when} ON {table} "
                    "BEGIN SELECT RAISE(ABORT, 'synthetic rejection'); END"
                )
            )


async def test_backfill_write_error_keeps_the_raw_record_out_of_the_logs(sessions, emitted):
    await reject(sessions, "raw_health_data")

    assert await backfill_service(sessions).run(["steps"]) == 0

    job = await backfill_job(sessions)
    # The window is paused as before: failed, one retry counted, a safe summary kept.
    assert (job.status, job.retry_count, job.last_error) == (
        JobStatus.failed,
        1,
        "IntegrityError",
    )
    assert_clean(emitted)
    (failed,) = emitted.events("BACKFILL_WINDOW_FAILED")
    assert failed["data_type"] == "steps" and failed["error_type"] == "IntegrityError"
    assert failed["error_chain"].startswith("IntegrityError < IntegrityError")
    assert "app.sync.backfill:_run_owned_job:" in failed["error_frames"]
    assert "app.services.archive.storage:" in failed["error_frames"]


class DriverError(Exception):
    sqlstate = "23505"


async def test_backfill_hides_the_database_detail_not_only_the_parameters(
    sessions, emitted, monkeypatch
):
    detail = f'duplicate key value violates unique constraint\nDETAIL:  Key (raw_json)=("{MARKER}")'

    async def failing_store(session, spec, page, **kwargs):
        # As app/db/session.py's engine raises it: parameters hidden, DETAIL kept.
        raise IntegrityError(
            "INSERT INTO raw_health_data ...",
            {"raw_json": page.data_points[0]},
            DriverError(detail),
            hide_parameters=True,
        )

    monkeypatch.setattr(ingestion, "store_page", failing_store)

    await backfill_service(sessions).run(["steps"])

    assert_clean(emitted)
    (failed,) = emitted.events("BACKFILL_WINDOW_FAILED")
    assert (failed["error_chain"], failed["sqlstate"]) == ("IntegrityError", "23505")
    assert (await backfill_job(sessions)).last_error == "IntegrityError"


async def test_identity_failure_keeps_source_metadata_out_of_logs_state_and_results(
    sessions, emitted
):
    await store(sessions, STEPS, steps(40, start=T0, source=fitbit(MARKER)))
    await reject(sessions, "raw_source_sightings")

    summary = await IdentityReconciler(sessions).run()

    assert summary is not None
    error = summary["steps"]["error"]
    assert error.startswith("IntegrityError < IntegrityError") and MARKER not in error
    async with sessions() as session:
        state = await session.get(IdentityReconcileState, "steps")
    assert state is not None and state.last_error == error
    assert_clean(emitted)
    (failed,) = emitted.events("IDENTITY_RECONCILE_DATA_TYPE_FAILED")
    assert failed["error_type"] == "IntegrityError"
    assert "app.services.identities.source_chain_repository:" in failed["error_frames"]
    # The scheduled job's result log repeats the stored summary, safe as well.
    (result,) = emitted.events("IDENTITY_RECONCILE_DATA_TYPE")
    assert result["error"] == error
    (completed,) = emitted.events("IDENTITY_RECONCILE_COMPLETED")
    assert completed["failed"] == 1

    # A run by hand (the CLI prints these results) gets the same safe summary.
    again = await identity_backfill(sessions, data_types=["steps"])
    assert again["steps"].error == error
    assert_clean(emitted)


async def test_reconciler_failure_outside_a_data_type_is_logged_without_its_text(
    sessions, emitted, monkeypatch
):
    async def failing(*args, **kwargs):
        raise IntegrityError("INSERT ...", {"data_source": MARKER}, DriverError(MARKER))

    monkeypatch.setattr(identity_reconciler, "identity_backfill", failing)

    assert await IdentityReconciler(sessions).run() is None

    assert_clean(emitted)
    (failed,) = emitted.events("IDENTITY_RECONCILE_FAILED")
    assert (failed["error_type"], failed["sqlstate"]) == ("IntegrityError", "23505")
    assert "app.services.identities.identity_reconciler:run:" in failed["error_frames"]


def test_a_busy_lock_keeps_its_own_text_for_status_and_health():
    busy = LockUnavailable(f"steps: {LOCK_ERROR} (or a row it needs) in 6 attempts")
    assert lock_failed(failure_summary(busy))
    private = IntegrityError("INSERT ...", {"data_source": MARKER}, DriverError(MARKER))
    assert failure_summary(private) == error_text(private) == "IntegrityError (SQLSTATE 23505)"


async def test_background_job_failure_logs_where_it_failed_not_its_message(emitted):
    def parse(value: str) -> int:
        return int(value)  # raises with the value in its message

    async def job() -> None:
        parse(MARKER)

    runtime = Runtime.__new__(Runtime)
    runtime.tasks = set()
    task = runtime.spawn(job())
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)  # the done callback

    assert_clean(emitted)
    (failed,) = emitted.events("BACKGROUND_JOB_FAILED")
    assert failed["error_type"] == "ValueError" and failed["error_chain"] == "ValueError"
    frames = failed["error_frames"].split(" > ")
    assert frames[-2].startswith("tests.test_log_privacy:job:")
    module, function, line = frames[-1].split(":")
    assert (module, function) == ("tests.test_log_privacy", "parse") and line.isdigit()


def test_library_records_with_exc_info_leave_without_the_exception_text(monkeypatch):
    # uvicorn's own plain-text handler, configured before the app (and its logging) starts.
    server_output, stdout = io.StringIO(), io.StringIO()
    logging.getLogger("uvicorn").addHandler(logging.StreamHandler(server_output))
    monkeypatch.setattr(sys, "stdout", stdout)
    configure_logging("INFO")
    monkeypatch.undo()

    try:
        raise IntegrityError("INSERT ...", {"raw_json": MARKER}, DriverError(MARKER))
    except IntegrityError:
        logging.getLogger("apscheduler.executors.default").exception(
            'Job "%s" raised an exception', "backfill"
        )
        logging.getLogger("uvicorn.error").exception("Exception in ASGI application\n")

    server = server_output.getvalue()
    assert MARKER not in server and "Traceback" not in server
    assert "Exception in ASGI application (error_type=IntegrityError" in server
    assert "sqlstate=23505" in server
    lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert MARKER not in stdout.getvalue()
    (job,) = [line for line in lines if line["name"] == "apscheduler.executors.default"]
    assert MARKER not in json.dumps(job) and "exc_info" not in job
    assert job["error_type"] == "IntegrityError" and job["sqlstate"] == "23505"
    assert job["error_frames"].startswith("tests.test_log_privacy:test_library_records")


def test_frames_name_modules_without_installation_paths():
    try:
        json.loads(MARKER)
    except ValueError as raised:
        error = raised
    where = error_frames(error)
    diagnostics = error_diagnostics(error, frames=True)
    assert diagnostics["error_frames"] == where
    assert where.startswith("tests.test_log_privacy:test_frames_name_modules")
    # The test's own frame, then where the standard library raised it.
    assert " > ... > json.decoder:raw_decode:" in where
    assert "/" not in where and "\\" not in where and MARKER not in where
    assert "error_frames" not in error_diagnostics(error)


async def test_application_engine_hides_bound_parameters():
    from app.core.config import Settings
    from app.db.engine import create_shared_engine

    engine = create_shared_engine(Settings(_env_file=None, database_url="sqlite+aiosqlite://"))

    # Ownership connections come from the same shared engine (app/db/engine.py).
    try:
        assert engine.sync_engine.hide_parameters
    finally:
        await engine.dispose()
