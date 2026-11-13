"""A copy of a signed delivery that was already processed costs nothing (synthetic data only).

Google's signature covers only the body and has no timestamp or nonce, so no freshness check is
possible without breaking it. The receiver remembers the (signature, body) pairs it processed
cleanly for a bounded time and answers their copies with 204 before any database write.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging

import pytest
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
    encode_dss_signature,
)
from sqlalchemy import func, select
from support.webhooks import Incremental, finish, make_client, notification
from support.webhooks import receiver as receiver

from app.google_health.webhook import REPLAY_WINDOW_SECONDS, ReplayWindow
from app.models.state import WebhookEvent
from app.models.webhook_status import WebhookStatusEvent


async def post(client, payload, signature="signed"):
    return await client.post(
        "/webhooks/google-health",
        content=json.dumps(payload).encode(),
        headers={
            "Authorization": "Bearer local-test",
            "GOOGLE-HEALTH-API-SIGNATURE": signature,
            "Content-Type": "application/json",
        },
    )


async def status_rows(sessions) -> int:
    async with sessions() as session:
        return await session.scalar(select(func.count()).select_from(WebhookStatusEvent))


async def test_copy_of_a_processed_delivery_writes_nothing(sessions, caplog):
    client, verifier, incremental, _, tasks = await make_client(sessions)
    payload = notification()
    async with client:
        assert (await post(client, payload)).status_code == 204
        await finish(tasks)
        before = await status_rows(sessions)
        with caplog.at_level(logging.INFO, logger="app.api.webhooks"):
            for _ in range(3):
                assert (await post(client, payload)).status_code == 204
                await finish(tasks)
    assert await status_rows(sessions) == before
    assert len(incremental.calls) == 1
    assert len(verifier.bodies) == 4  # every copy is still verified before it is answered
    assert [r.event for r in caplog.records].count("WEBHOOK_REPLAY_SUPPRESSED") == 3
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(WebhookEvent)) == 1


async def test_a_failed_batch_stays_redeliverable(sessions):
    client, _, incremental, _, tasks = await make_client(sessions, Incremental(fail_once={"steps"}))
    payload = notification()
    async with client:
        await post(client, payload)
        await finish(tasks)
        before = await status_rows(sessions)
        assert (await post(client, payload)).status_code == 204
        await finish(tasks)
    # The re-delivery restarted the failed element, as before this window existed.
    assert len(incremental.calls) == 2
    assert await status_rows(sessions) > before


async def test_an_unsigned_copy_is_still_refused(sessions):
    client, _, _, _, tasks = await make_client(sessions)
    payload = notification()
    async with client:
        await post(client, payload)
        await finish(tasks)
        response = await client.post(
            "/webhooks/google-health",
            content=json.dumps(payload).encode(),
            headers={"Authorization": "Bearer local-test", "Content-Type": "application/json"},
        )
    assert response.status_code == 401


def test_window_binds_signature_to_body_and_expires():
    now = [0.0]
    window = ReplayWindow(clock=lambda: now[0])
    window.remember("sig", b"body")
    assert window.seen("sig", b"body")
    assert not window.seen("sig", b"other body")
    assert not window.seen("other", b"body")
    now[0] += REPLAY_WINDOW_SECONDS
    assert not window.seen("sig", b"body")
    assert window.suppressed == 1


def test_window_stays_bounded_and_drops_the_oldest():
    now = [0.0]
    window = ReplayWindow(clock=lambda: now[0], max_entries=3)
    for index in range(5):
        now[0] += 1
        window.remember(f"sig-{index}", b"body")
    assert len(window._expiry) == 3
    assert not window.seen("sig-0", b"body") and not window.seen("sig-1", b"body")
    assert all(window.seen(f"sig-{index}", b"body") for index in (2, 3, 4))


@pytest.mark.parametrize("variant", ["ecdsa", "base64"])
async def test_equivalent_signature_is_suppressed_without_writes(receiver, sessions, variant):
    body = json.dumps(notification()).encode()
    # Select a DER length with unused base64 padding bits.
    for _ in range(32):
        signature = receiver.sign(body)
        if signature.endswith("="):
            break
    assert signature.endswith("=")
    signed = base64.b64decode(signature, validate=True)
    if variant == "ecdsa":
        order = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
        r, s = decode_dss_signature(signed[5:])
        twin = base64.b64encode(signed[:5] + encode_dss_signature(r, order - s)).decode()
    else:
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
        index = len(signature.rstrip("=")) - 1
        neighbor = alphabet[alphabet.index(signature[index]) + 1]
        twin = signature[:index] + neighbor + signature[index + 1 :]
        assert base64.b64decode(twin, validate=True) == signed
    assert twin != signature
    verifier = receiver.app.state.runtime.webhook_verifier
    await verifier.verify(body, signature)
    await verifier.verify(body, twin)
    window = ReplayWindow()
    window.remember(signature, body)
    assert window.seen(twin, body)
    assert not window.seen(twin, body + b" ")
    # A fresh signature or a different key ID must retain its own cache entry.
    assert not window.seen(receiver.sign(body), body)
    other_key = base64.b64encode(signed[:1] + (43).to_bytes(4, "big") + signed[5:]).decode()
    assert not window.seen(other_key, body)
    assert (await receiver.post(body, signature=signature)).status_code == 204
    await receiver.finish()
    before = await status_rows(sessions)
    assert (await receiver.post(body, signature=twin)).status_code == 204
    await receiver.finish()
    assert await status_rows(sessions) == before
    assert len(receiver.incremental.calls) == 1


@pytest.mark.parametrize(
    "signature",
    [
        "not-base64!",
        "ğ",
        "",
        "YQ==",
        "AQAAAAE=",
        base64.b64encode(b"\x01\0\0\0\x01bad-DER").decode(),
    ],
)
def test_invalid_signature_falls_back_to_stripped_text_key(signature):
    body = b"synthetic body"
    digest = hashlib.sha256(signature.encode())
    digest.update(b"\0" + hashlib.sha256(body).digest())
    assert ReplayWindow._key(f" {signature} ", body) == digest.hexdigest()
