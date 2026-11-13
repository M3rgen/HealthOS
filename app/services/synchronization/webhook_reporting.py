"""Secret-safe webhook receipt and processing totals."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.state import WebhookEvent, WebhookNotification


async def webhook_status(session: AsyncSession) -> dict:
    event_count = await session.scalar(select(func.count(WebhookEvent.payload_hash)))
    pending_count = await session.scalar(
        select(func.count(WebhookEvent.payload_hash)).where(WebhookEvent.processed_at.is_(None))
    )
    last_received = await session.scalar(select(func.max(WebhookEvent.created_at)))
    last_processed = await session.scalar(select(func.max(WebhookEvent.processed_at)))
    notification_count = await session.scalar(select(func.count(WebhookNotification.element_hash)))
    notification_states = await session.execute(
        select(WebhookNotification.status, func.count()).group_by(WebhookNotification.status)
    )
    sync_attempts = await session.scalar(
        select(func.coalesce(func.sum(WebhookNotification.sync_attempt_count), 0))
    )
    sync_successes = await session.scalar(
        select(func.coalesce(func.sum(WebhookNotification.sync_success_count), 0))
    )
    return {
        "received_events": event_count,
        "pending_events": pending_count,
        "last_received_at": str(last_received) if last_received else None,
        "last_processed_at": str(last_processed) if last_processed else None,
        "notification_elements": notification_count,
        "notification_states": {row[0]: row[1] for row in notification_states},
        "incremental_sync_attempts": sync_attempts,
        "incremental_sync_successes": sync_successes,
    }
