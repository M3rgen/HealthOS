"""Plan persistence; callers own transactions. No document content is logged."""

import logging
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import event
from app.db.dialect import database, dialect_insert
from app.db.postgres import LOCK_NAMESPACES
from app.models.plan import PlanState, PlanVersion
from app.schemas.plan import PlanStateBody, PlanStateView, PlanView
from app.services.operations.plan_validation import (
    InvalidPlan,
    content_digest,
    section_counts,
    shopping_ids,
    validate_document,
)

logger = logging.getLogger(__name__)


async def lock_plan(session: AsyncSession) -> None:
    # Serializes imports and state replacements, including when no singleton exists yet.
    await database(session).credential_lock(session, LOCK_NAMESPACES["plan"])


async def active_version(session: AsyncSession) -> PlanVersion | None:
    return await session.scalar(select(PlanVersion).order_by(PlanVersion.id.desc()).limit(1))


async def import_document(session: AsyncSession, document: dict) -> tuple[PlanVersion, bool]:
    validate_document(document)
    digest = content_digest(document)
    await lock_plan(session)
    existing = await session.scalar(select(PlanVersion).where(PlanVersion.content_sha256 == digest))
    if existing is not None:
        return existing, False
    row = PlanVersion(content=document, content_sha256=digest, schema_version=1)
    session.add(row)
    await session.flush()
    return row, True


def state_response(row: PlanState | None, known: set[str]) -> PlanStateView:
    return {
        "start_date": row.start_date.isoformat() if row and row.start_date else None,
        # Old versions' ids remain stored; only the active plan's ids are shown.
        "checked_item_ids": [item for item in row.checked_item_ids if item in known] if row else [],
    }


async def get_plan(session: AsyncSession) -> PlanView:
    version = await active_version(session)
    if version is None:
        return {"plan": None}
    parsed = validate_document(version.content)
    state = await session.get(PlanState, 1)
    return {
        "plan": version.content,
        "version": version.id,
        "schema_version": version.schema_version,
        "imported_at": version.imported_at,
        "state": state_response(state, set(shopping_ids(parsed.shopping))),
    }


async def put_state(session: AsyncSession, body: PlanStateBody) -> PlanStateView:
    await lock_plan(session)
    version = await active_version(session)
    known = set(shopping_ids(validate_document(version.content).shopping)) if version else set()
    if version is None:
        raise InvalidPlan("$", "no_plan")
    if set(body.checked_item_ids) - known:
        raise InvalidPlan("$.checked_item_ids", "unknown_identifier")
    if len(body.checked_item_ids) != len(set(body.checked_item_ids)):
        raise InvalidPlan("$.checked_item_ids", "duplicate_identifier")
    values = {
        "schema_version": 1,
        "start_date": date.fromisoformat(body.start_date) if body.start_date else None,
        "checked_item_ids": body.checked_item_ids,
        "updated_at": datetime.now(UTC),
    }
    statement = dialect_insert(session, PlanState).values(id=1, **values)
    await session.execute(
        statement.on_conflict_do_update(index_elements=[PlanState.id], set_=values)
    )
    return {"start_date": body.start_date, "checked_item_ids": body.checked_item_ids}


def log_import(version: PlanVersion, added: bool) -> None:
    event(logger, "PLAN_IMPORT", version=version.id, added=added, **section_counts(version.content))
