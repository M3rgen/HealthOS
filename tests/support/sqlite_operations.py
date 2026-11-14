"""Database operations required only by synthetic SQLite tests.

Production operations remain PostgreSQL code. These alternatives are selected explicitly
by the adapter installed from conftest, never by application startup.
"""

import hashlib
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager

from sqlalchemy import select, text

from app.models.raw_versions import RawApiResponse, RawHealthDataVersion
from app.models.state import RawHealthData
from app.parsers.identity import google_name, record_identity
from app.services.identities.identity_backfill_reading import spec_for
from support.sqlite_guards import SQLITE_TRIGGER_SQL
from support.sqlite_ownership import _SqliteLock


async def no_errors(*args, **kwargs):
    return []


async def no_lock(*args, **kwargs):
    pass


async def denied(*args, **kwargs):
    return False


async def granted(*args, **kwargs):
    return True


async def no_rejections(*args, **kwargs):
    return set()


async def no_role_risk(*args, **kwargs):
    return None


@asynccontextmanager
async def run_lock(*args, **kwargs):
    yield


@asynccontextmanager
async def recovery_lock(*args, **kwargs):
    yield True


async def verify_trigger_definitions(session):
    errors = []
    stored = {
        (table, trigger): sql
        for table, trigger, sql in (
            await session.execute(
                text("SELECT tbl_name, name, sql FROM sqlite_master WHERE type = 'trigger'")
            )
        ).tuples()
    }
    for trigger, (table, sql) in SQLITE_TRIGGER_SQL.items():
        if (table, trigger) not in stored:
            errors.append(f"protective trigger missing: {table}.{trigger}")
        elif " ".join(stored[(table, trigger)].split()) != " ".join(sql.split()):
            errors.append(f"protective trigger {table}.{trigger} was redefined")
    return errors


async def response_body_mismatches(session):
    errors = []
    rows = await session.stream(
        select(RawApiResponse.id, RawApiResponse.body, RawApiResponse.body_sha256)
        .order_by(RawApiResponse.id)
        .execution_options(yield_per=200)
    )
    async for row_id, body, expected in rows:
        if hashlib.sha256(body).hexdigest() != expected:
            errors.append(f"stored Google response {row_id} no longer matches its SHA-256")
    rows = await session.execute(
        text("SELECT payload_hash, raw_body FROM webhook_events WHERE raw_body IS NOT NULL")
    )
    webhooks = [
        digest for digest, body in rows.tuples() if hashlib.sha256(body).hexdigest() != digest
    ]
    return errors, webhooks


async def foreign_history(session):
    found: dict[str, list[uuid.UUID]] = defaultdict(list)
    result = await session.stream(
        select(
            RawHealthData.data_type,
            RawHealthData.id,
            RawHealthData.raw_json,
            RawHealthDataVersion.raw_json,
        ).join(RawHealthDataVersion, RawHealthDataVersion.raw_id == RawHealthData.id)
    )
    async for data_type, raw_id, current, earlier in result:
        if google_name(current) or google_name(earlier):
            continue
        spec = spec_for(data_type)
        now, then = record_identity(spec, current), record_identity(spec, earlier)
        if now is not None and then is not None and now.source != then.source:
            found[data_type].append(raw_id)
    return [
        f"{len(set(raw_ids))} raw row(s) of {data_type} hold payloads of more than one source "
        f"in their history (e.g. {sorted(set(raw_ids), key=str)[0]}): one source's payload "
        "was written onto another source's row"
        for data_type, raw_ids in sorted(found.items())
    ]


def refuse_while_held(operations, table, schema, where, what):
    from app.db.downgrade_guard import _quoted, _refused

    if operations.migration_context.as_sql:
        return
    target = _quoted(operations, table, schema)
    condition = f" WHERE {where}" if where else ""
    bind = operations.get_bind()
    if bind.scalar(text(f"SELECT 1 FROM {target}{condition} LIMIT 1")) is not None:
        raise RuntimeError(_refused(what))


OPERATIONS = {
    "app.services.readers.heart_maintenance.fence": denied,
    "app.services.readers.heart_projection.packets": no_role_risk,
    "app.sync.ownership._run_lock": _SqliteLock,
    "app.db.downgrade_guard._refuse_while_held": refuse_while_held,
    "app.services.identities.source_chain_locks.lock_data_type": no_lock,
    "app.services.identities.source_chain_locks.try_lock_deferrals": granted,
    "app.services.identities.source_chain_locks.backfill_lock": run_lock,
    "app.services.identities.identity_schema.identity_tables_missing": no_errors,
    "app.services.identities.identity_schema.verify_identity_schema": no_errors,
    "app.db.role_check.database_role_risk": no_role_risk,
    "app.services.archive.raw_history_verification.verify_trigger_definitions": (
        verify_trigger_definitions
    ),
    "app.services.archive.responses.response_body_mismatches": response_body_mismatches,
    "app.services.identities.identity_verification._index_notes": no_errors,
    "app.services.identities.identity_verification._foreign_history": foreign_history,
    "app.services.archive.raw_history_writing.jsonb_rejects": no_rejections,
    "app.services.interpretation.derived.build_fence": granted,
    "app.sync.watchdog.recovery_lock": recovery_lock,
    "app.services.archive.manual_flags._hold_flag_writer": no_lock,
    "app.services.archive.manual_flags._date_after": no_lock,
}
