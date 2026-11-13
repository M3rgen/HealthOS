"""Run SQLite-default suites against a migrated PostgreSQL: ``make test-postgres``.

With ``HEALTHOS_TEST_BACKEND=postgres`` the shared ``sessions`` fixture hands out a throwaway
database cloned from a template that alembic migrated to head, in the production schema, so the
suite meets PostgreSQL's locking, types, triggers and constraints instead of SQLite's.
"""

from __future__ import annotations

import atexit
import logging
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings

BACKEND = os.environ.get("HEALTHOS_TEST_BACKEND", "sqlite")
# A server time zone other than UTC proves the application never relies on it.
SESSION_TIME_ZONE = "America/Los_Angeles"

_template: str | None = None

# Tests that describe SQLite itself and cannot run on PostgreSQL, grouped by why. Guards and
# triggers have their PostgreSQL counterpart in tests/integration. A new test runs on
# PostgreSQL too unless it is added here.
SQLITE_SHAPED = {
    "freezes SQLite's bytes; PostgreSQL gives the same values with Z and jsonb key order": (
        "test_frozen_http_json",
        "test_frozen_dashboard_and_canonical_json",
    ),
    "exercises a SQLite trigger; PostgreSQL's are covered in tests/integration": (
        "test_identity_tables_are_append_only_on_sqlite",
        "test_sightings_only_widen_on_sqlite",
        "test_verify_reports_a_missing_identity_guard",
        "test_flag_events_and_measurements_refuse_update_and_delete",
        "test_verify_checks_the_flag_guards",
        "test_responses_and_events_are_append_only",
        "test_verify_reports_a_redefined_sqlite_guard",
        "test_history_rows_are_append_only",
        "test_raw_records_cannot_be_deleted_or_rekeyed_on_sqlite",
        "test_verify_catches_tampered_history",
        "test_manual_entries_are_append_only",
        "test_webhook_payloads_are_frozen_but_processing_state_is_not",
        "test_verifier_checks_new_sqlite_triggers",
        "test_verify_reports_missing_or_redefined_sqlite_guards",
        "test_sqlite_evidence_is_append_only",
    ),
    "provokes a failure through SQLite trigger syntax or a dropped SQLite trigger": (
        "test_interruption_after_reset_keeps_checkpoint_and_attempt_for_continuation",
        "test_page_or_pointer_failure_rolls_back_records_and_checkpoint",
        "test_reset_moves_boundary_and_only_new_attempt_keeps_records_live",
        "test_reset_transaction_rollback_preserves_old_attempt",
        "test_write_verification_failure_rolls_back_inserts_and_renumbering",
        "test_archive_failure_is_private_and_preserves_outcome",
        "test_backfill_write_error_keeps_the_raw_record_out_of_the_logs",
        "test_identity_failure_keeps_source_metadata_out_of_logs_state_and_results",
        "test_change_during_calculation_is_refused_instead_of_mixing_snapshots",
    ),
    "counts SQL statements, which PostgreSQL's plan settings change": (
        "test_history_archive_date_conversion_overflow",
        "test_a_page_records_its_sightings_last_with_few_statements",
        "test_sqlite_large_reads_keep_pairs_order_latest_and_global_flag_budget",
    ),
    "compares SQLite's naive timestamps or seeds rows a migration already wrote": (
        "test_sightings_keep_their_label_and_source",
        "test_persisted_differences_equal_http_results_including_unchanged",
        "test_failed_report_never_exposes_upstream_exception_details",
        "test_watchdog_cooldown_prevents_recovery_loop",
    ),
    "stores 1e309, which only SQLite keeps; NUMERIC(12,4) refuses it at insert": (
        "test_profile_conversions_keep_latest_missing_value_and_zero",
    ),
}
_SHAPED_NAMES = frozenset(name for names in SQLITE_SHAPED.values() for name in names)


def split_sqlite_shaped(items) -> tuple[list, list]:
    keep, shaped = [], []
    for item in items:
        (shaped if getattr(item, "originalname", None) in _SHAPED_NAMES else keep).append(item)
    return keep, shaped


def _server():
    raw = os.environ.get("DATABASE_URL", "")
    url = make_url(raw) if raw else None
    if url is None or url.get_backend_name() != "postgresql":
        pytest.fail("HEALTHOS_TEST_BACKEND=postgres needs DATABASE_URL pointing at PostgreSQL")
    return url


def _admin():
    return create_engine(
        _server().set(drivername="postgresql+psycopg").render_as_string(hide_password=False),
        isolation_level="AUTOCOMMIT",
    )


@contextmanager
def _logging_untouched() -> Iterator[None]:
    """migrations/env.py's fileConfig() replaces the root handlers and disables loggers; the test
    that happens to build the template in its process must keep its caplog and loggers."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    loggers = [
        item
        for item in logging.Logger.manager.loggerDict.values()
        if isinstance(item, logging.Logger)
    ]
    disabled = [item.disabled for item in loggers]
    try:
        yield
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)
        for item, was_disabled in zip(loggers, disabled, strict=True):
            item.disabled = was_disabled


def template_database() -> str:
    """One migrated template per test process: cloning it costs milliseconds, not a migration."""
    global _template
    if _template is None:
        name = f"healthos_tpl_{uuid.uuid4().hex[:12]}"
        admin = _admin()
        try:
            with admin.connect() as connection:
                connection.execute(text(f'CREATE DATABASE "{name}"'))
            url = _server().set(drivername="postgresql+asyncpg", database=name)
            previous = os.environ.get("DATABASE_URL")
            os.environ["DATABASE_URL"] = url.render_as_string(hide_password=False)
            get_settings.cache_clear()
            try:
                with _logging_untouched():
                    command.upgrade(Config("alembic.ini"), "head")
            finally:
                os.environ["DATABASE_URL"] = previous or ""
                get_settings.cache_clear()
        finally:
            admin.dispose()
        _template = name
        atexit.register(_drop_template, name)
    return _template


def _drop_template(name: str) -> None:
    admin = _admin()
    try:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        admin.dispose()


@asynccontextmanager
async def postgres_sessions() -> AsyncIterator[async_sessionmaker]:
    name = f"healthos_it_{uuid.uuid4().hex[:12]}"
    admin = _admin()
    template = template_database()
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}" TEMPLATE "{template}"'))
        url = _server().set(drivername="postgresql+asyncpg", database=name)
        engine = create_async_engine(
            url,
            connect_args={"server_settings": {"TimeZone": SESSION_TIME_ZONE}},
        )
        try:
            yield async_sessionmaker(engine, expire_on_commit=False)
        finally:
            await engine.dispose()
    finally:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()
