"""Version import transactions and immutable content, using only the synthetic example."""

from copy import deepcopy

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from support.plans import sample

from app.models.plan import PlanState, PlanVersion
from app.services.operations.plan_validation import InvalidPlan
from app.services.operations.plans import get_plan, import_document


async def test_import_deduplicates_all_history_and_keeps_newest_active(sessions):
    document = sample()
    async with sessions.begin() as session:
        first, added = await import_document(session, document)
        assert added
    second_document = deepcopy(document)
    second_document["meta"]["title"] = "İkinci sentetik örnek"
    async with sessions.begin() as session:
        second, added = await import_document(session, second_document)
        assert added and second.id > first.id
    for payload, expected in ((document, first), (second_document, second)):
        async with sessions.begin() as session:
            repeated, added = await import_document(session, payload)
            assert not added and repeated.id == expected.id
    async with sessions() as session:
        versions = (await session.scalars(select(PlanVersion).order_by(PlanVersion.id))).all()
        assert [row.content for row in versions] == [document, second_document]
        assert all(row.schema_version == 1 and row.imported_at for row in versions)
        response = await get_plan(session)
        assert response["version"] == second.id
        assert response["plan"] == second_document
        assert await session.get(PlanState, 1) is None


async def test_invalid_import_and_caller_rollback_write_nothing(sessions):
    async with sessions.begin() as session:
        first, _ = await import_document(session, sample())
    async with sessions() as session:
        invalid = sample()
        invalid["meta"]["title"] = "x" * 201
        with pytest.raises(InvalidPlan):
            await import_document(session, invalid)
        assert len((await session.scalars(select(PlanVersion))).all()) == 1
    async with sessions() as session:
        rolled_back, _ = await import_document(session, {"schema": 1})
        assert rolled_back.id != first.id
        await session.rollback()
    async with sessions() as session:
        assert (await session.scalars(select(PlanVersion.id))).all() == [first.id]


@pytest.mark.parametrize(
    "statement", ["DELETE FROM plan_versions", "UPDATE plan_versions SET id=id"]
)
async def test_database_refuses_overwriting_or_deleting_plan_versions(sessions, statement):
    async with sessions.begin() as session:
        await import_document(session, sample())
    async with sessions() as session:
        with pytest.raises(DBAPIError, match="keeps every record"):
            await session.execute(text(statement))
        await session.rollback()
        assert (await session.scalar(select(PlanVersion.content))) == sample()
