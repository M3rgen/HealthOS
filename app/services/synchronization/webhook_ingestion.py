"""Materialize notification elements without changing the signed batch archive."""

import hashlib

from sqlalchemy import null, select

from app.core.webhook_types import NotificationStatus
from app.db.dialect import dialect_insert
from app.models.oauth_credentials import current_credential
from app.models.state import WebhookBatchItem, WebhookNotification
from app.parsers.common import canonical_json

from .webhook_metadata import (
    UNPARSEABLE,
    UNPARSED_BODY,
    UNREPRESENTABLE,
    NotificationWindowError,
    extract_notification,
    index_metadata,
    jsonb_storable,
    notification_windows,
)


async def prepare_elements(session, batch_hash, elements, supported_data_types, specs, status):
    credential = await current_credential(session)
    expected_user_id = credential.health_user_id if credential else None
    for ordinal, item in enumerate(elements, start=1):
        # Without parsed boundaries the whole body is one rejected item.
        # Domain separation avoids colliding with a canonical JSON hash;
        # different raw bodies must never collapse onto a shared sentinel.
        element_digest = (
            hashlib.sha256(f"unparsed:{batch_hash}".encode()).hexdigest()
            if item is UNPARSED_BODY
            else _element_hash(item)
        )
        storable = jsonb_storable(item)
        # An element jsonb cannot hold keeps nothing derived; its bytes stay
        # in the batch raw_body at this ordinal.
        extracted, validation_error = (
            extract_notification(item, expected_user_id, supported_data_types)
            if storable
            else ({}, UNPARSEABLE if item is UNPARSED_BODY else UNREPRESENTABLE)
        )
        if validation_error is None:
            try:
                notification_windows(
                    specs[extracted["dataType"]],
                    extracted["intervals"],
                )
            except NotificationWindowError as exc:
                validation_error = str(exc)
        is_valid = validation_error is None
        if not is_valid and status:
            status.observe(session, "notification_rejected")
        values = {
            "element_hash": element_digest,
            "raw_json": item if storable else null(),
            **index_metadata(extracted),
            "intervals": extracted.get("intervals"),
            "is_valid": is_valid,
            "status": NotificationStatus.pending if is_valid else NotificationStatus.rejected,
            "attempt_count": 0,
            "sync_attempt_count": 0,
            "sync_success_count": 0,
            "validation_error": validation_error,
        }
        await session.execute(
            dialect_insert(session, WebhookNotification)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["element_hash"])
        )
        await session.execute(
            dialect_insert(session, WebhookBatchItem)
            .values(
                batch_hash=batch_hash,
                ordinal=ordinal,
                element_hash=element_digest,
            )
            .on_conflict_do_nothing(index_elements=["batch_hash", "ordinal"])
        )


def _element_hash(item: object) -> str:
    # payload_hash, which cannot encode an unpaired surrogate; equal for every other value.
    return hashlib.sha256(canonical_json(item).encode("utf-8", "surrogatepass")).hexdigest()


async def has_valid_element(session, batch_hash):
    return await session.scalar(
        select(WebhookBatchItem.element_hash)
        .join(
            WebhookNotification,
            WebhookNotification.element_hash == WebhookBatchItem.element_hash,
        )
        .where(
            WebhookBatchItem.batch_hash == batch_hash,
            WebhookNotification.is_valid.is_(True),
        )
        .limit(1)
    )
