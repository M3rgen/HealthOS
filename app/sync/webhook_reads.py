"""Bound archive scans without holding a database session while processing work."""

from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import Row, Select, select, tuple_
from sqlalchemy.orm import InstrumentedAttribute

from app.core.webhook_types import NotificationStatus, notification_status
from app.db.contracts import SessionFactory
from app.models.state import WebhookBatchItem, WebhookEvent, WebhookNotification

READ_PAGE_SIZE = 256


async def read_pages(
    sessions: SessionFactory, statement: Select[Any], *keys: InstrumentedAttribute[Any]
) -> AsyncIterator[Sequence[Row[Any]]]:
    """Key columns must lead the projection and uniquely order rows, without changing.

    Each page releases its session before yielding: callbacks can commit on another
    connection. A keyset advances even when processing removes rows from the selection.
    The initial upper key keeps new deliveries from extending a running archive scan.
    """
    async with sessions() as session:
        upper = (
            await session.execute(statement.order_by(*(k.desc() for k in keys)).limit(1))
        ).first()
    if upper is None:
        return
    bounded = statement.where(tuple_(*keys) <= tuple(upper[: len(keys)]))
    cursor = None
    while True:
        page_query = bounded if cursor is None else bounded.where(tuple_(*keys) > cursor)
        async with sessions() as session:
            rows = (await session.execute(page_query.order_by(*keys).limit(READ_PAGE_SIZE))).all()
        if not rows:
            return
        cursor = tuple(rows[-1][: len(keys)])
        yield rows
        if len(rows) < READ_PAGE_SIZE:
            return


async def batch_hashes(sessions: SessionFactory, *, unprepared: bool = False) -> AsyncIterator[str]:
    query = select(WebhookEvent.created_at, WebhookEvent.payload_hash)
    if unprepared:
        query = query.where(
            ~select(WebhookBatchItem.batch_hash)
            .where(WebhookBatchItem.batch_hash == WebhookEvent.payload_hash)
            .exists()
        )
    else:
        query = query.where(WebhookEvent.processed_at.is_(None))
    async for page in read_pages(
        sessions, query, WebhookEvent.created_at, WebhookEvent.payload_hash
    ):
        for row in page:
            yield row.payload_hash


async def unfinished_hashes(sessions: SessionFactory, cutoff: datetime) -> AsyncIterator[str]:
    query = select(
        WebhookNotification.updated_at,
        WebhookNotification.element_hash,
        WebhookNotification.status,
        WebhookNotification.last_error,
    ).where(
        WebhookNotification.is_valid.is_(True),
        WebhookNotification.status.in_(
            (NotificationStatus.pending, NotificationStatus.interrupted, NotificationStatus.failed)
        ),
        WebhookNotification.updated_at < cutoff,
    )
    async for page in read_pages(
        sessions, query, WebhookNotification.updated_at, WebhookNotification.element_hash
    ):
        for row in page:
            status = notification_status(row.status, row.last_error)
            if status in (NotificationStatus.pending, NotificationStatus.interrupted):
                yield row.element_hash
