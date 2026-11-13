"""Retrieval receipt test helpers."""

from __future__ import annotations

from sqlalchemy import select

from app.google_health.data_types import REGISTRY
from app.models.retrieval import RetrievalEvent as Event
from app.models.retrieval import RetrievalReceipt as Receipt
from app.services.shared.query_ranges import CivilDateRange
from app.services.synchronization.retrieval_coverage import coverage_report


async def rows(sessions):
    async with sessions() as session:
        return (
            list((await session.scalars(select(Receipt).order_by(Receipt.started_at))).all()),
            list((await session.scalars(select(Event).order_by(Event.id))).all()),
        )


async def status(sessions, runner):
    async with sessions() as session:
        report = await coverage_report(
            session,
            REGISTRY,
            runner.kind,
            dates=CivilDateRange(runner.start.date(), runner.start.date(), "UTC"),
        )
    return report["types"][0]["days"][0]
