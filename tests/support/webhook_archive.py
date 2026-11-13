"""Webhook archive test helpers: the stored rows of a batch and a snapshot of them."""

from __future__ import annotations

from sqlalchemy import select, text

from app.models.state import WebhookBatchItem, WebhookEvent, WebhookNotification

KINDS = ["nul_value", "nul_key", "nul_data_type", "surrogate", "nan", "overflow"]


async def rows(sessions):
    async with sessions() as session:
        batches = (await session.scalars(select(WebhookEvent))).all()
        elements = (await session.scalars(select(WebhookNotification))).all()
        items = (
            await session.scalars(
                select(WebhookBatchItem).order_by(
                    WebhookBatchItem.batch_hash, WebhookBatchItem.ordinal
                )
            )
        ).all()
    return batches, elements, items


async def snapshot(sessions):
    async with sessions() as session:
        return [
            (await session.execute(text(f"SELECT {columns} FROM {table} ORDER BY 1"))).all()
            for table, columns in (
                ("webhook_events", "payload_hash, raw_json, raw_body, created_at"),
                ("webhook_notifications", "element_hash, raw_json, is_valid, created_at"),
            )
        ]
