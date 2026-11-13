import asyncio
import base64
import gzip
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from compressed_records import compressed
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from fastapi.testclient import TestClient
from http_close_records import ClosingStream, dispose, repeated_cancel, safe_chain
from support.webhooks import receiver as receiver

from app.api import webhooks
from app.core.config import Settings
from app.core.request_limits import WriteLimits
from app.google_health.webhook import WebhookSignatureVerifier, WebhookVerificationError

KEYSET_URL = "https://www.gstatic.com/googlehealthapi/webhooks/webhooks_public_keyset.json"


def _field(number, value):
    return bytes([(number << 3) | 2, len(value)]) + value


async def test_webhook_tink_signature_verification():
    private = ec.generate_private_key(ec.SECP256R1())
    numbers = private.public_key().public_numbers()
    x = numbers.x.to_bytes(32, "big")
    y = numbers.y.to_bytes(32, "big")
    serialized_proto = bytes([8, 0]) + _field(3, x) + _field(4, y)
    key_id = 12345
    keyset = {
        "key": [
            {
                "keyId": key_id,
                "status": "ENABLED",
                "keyData": {"value": base64.b64encode(serialized_proto).decode()},
            }
        ]
    }

    def handler(request):
        return httpx.Response(200, json=keyset)

    body = b'{"data":{"dataType":"steps"}}'
    der = private.sign(body, ec.ECDSA(hashes.SHA256()))
    signed = bytes([1]) + key_id.to_bytes(4, "big") + der
    verifier = WebhookSignatureVerifier(
        KEYSET_URL, httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    await verifier.verify(body, base64.b64encode(signed).decode())
    with pytest.raises(WebhookVerificationError):
        await verifier.verify(body + b"x", base64.b64encode(signed).decode())


async def test_webhook_tink_signature_verification_with_batched_body():
    private = ec.generate_private_key(ec.SECP256R1())
    numbers = private.public_key().public_numbers()
    serialized_proto = (
        bytes([8, 0])
        + _field(3, numbers.x.to_bytes(32, "big"))
        + _field(4, numbers.y.to_bytes(32, "big"))
    )
    key_id = 24680
    keyset = {
        "key": [
            {
                "keyId": key_id,
                "status": "ENABLED",
                "keyData": {"value": base64.b64encode(serialized_proto).decode()},
            }
        ]
    }

    verifier = WebhookSignatureVerifier(
        KEYSET_URL,
        httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=keyset))
        ),
    )
    body = b'[{"data":{"dataType":"steps"}},{"data":{"dataType":"heart-rate"}}]'
    signature = private.sign(body, ec.ECDSA(hashes.SHA256()))
    signed = bytes([1]) + key_id.to_bytes(4, "big") + signature

    await verifier.verify(body, base64.b64encode(signed).decode())
    with pytest.raises(WebhookVerificationError):
        await verifier.verify(body + b" ", base64.b64encode(signed).decode())


async def test_unknown_cached_webhook_key_forces_rotation_refresh():
    now = 0
    private = ec.generate_private_key(ec.SECP256R1())
    numbers = private.public_key().public_numbers()
    serialized_proto = (
        bytes([8, 0])
        + _field(3, numbers.x.to_bytes(32, "big"))
        + _field(4, numbers.y.to_bytes(32, "big"))
    )
    key_id = 9876
    rotated = {
        "key": [
            {
                "keyId": key_id,
                "status": "ENABLED",
                "keyData": {"value": base64.b64encode(serialized_proto).decode()},
            }
        ]
    }
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"key": []} if calls == 1 else rotated)

    verifier = WebhookSignatureVerifier(
        KEYSET_URL,
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        monotonic=lambda: now,
    )
    await verifier._get_keyset()
    now = 60
    body = b'{"data":{"dataType":"steps"}}'
    signature = private.sign(body, ec.ECDSA(hashes.SHA256()))
    signed = bytes([1]) + key_id.to_bytes(4, "big") + signature

    await verifier.verify(body, base64.b64encode(signed).decode())

    assert calls == 2


@pytest.mark.parametrize(
    "url",
    [
        "https://keys.example.com/keyset.json",
        "http://www.gstatic.com/googlehealthapi/webhooks/webhooks_public_keyset.json",
        "https://www.gstatic.com.example.com/keyset.json",
        "https://www.gstatic.com@example.com/keyset.json",
    ],
)
def test_keyset_url_must_be_pinned_to_google_static_host(url):
    with pytest.raises(ValueError, match="gstatic"):
        Settings(google_health_webhook_public_key_url=url, _env_file=None)
    with pytest.raises(ValueError, match="gstatic"):
        WebhookSignatureVerifier(url)


def test_default_keyset_url_is_accepted():
    settings = Settings(_env_file=None)
    assert settings.google_health_webhook_public_key_url == KEYSET_URL


@pytest.fixture
def signed_webhook():
    private = ec.generate_private_key(ec.SECP256R1())
    numbers = private.public_key().public_numbers()
    serialized = _field(3, numbers.x.to_bytes(32, "big")) + _field(4, numbers.y.to_bytes(32, "big"))
    keyset = {
        "key": [
            {
                "keyId": 42,
                "status": "ENABLED",
                "keyData": {"value": base64.b64encode(serialized).decode()},
            }
        ]
    }
    body = b'{"data":{"dataType":"steps"}}'
    signed = b"\x01" + (42).to_bytes(4, "big") + private.sign(body, ec.ECDSA(hashes.SHA256()))
    return body, signed, keyset


@pytest.mark.parametrize("prefix", [0, 2, 255])
async def test_non_tink_prefix_rejects_otherwise_valid_signature(signed_webhook, prefix):
    body, signed, keyset = signed_webhook
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=keyset))
    ) as http:
        verifier = WebhookSignatureVerifier(KEYSET_URL, http)
        with pytest.raises(WebhookVerificationError) as error:
            await verifier.verify(body, base64.b64encode(bytes([prefix]) + signed[1:]).decode())
        assert error.value.reason == "malformed_signature"


async def test_disabled_key_rejects_its_valid_signature(signed_webhook):
    body, signed, keyset = signed_webhook
    signature = base64.b64encode(signed).decode()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=keyset))
    ) as http:
        await WebhookSignatureVerifier(KEYSET_URL, http).verify(body, signature)
        keyset["key"][0]["status"] = "DISABLED"
        now = 0
        verifier = WebhookSignatureVerifier(KEYSET_URL, http, monotonic=lambda: now)
        await verifier._get_keyset()
        now = 60
        with pytest.raises(WebhookVerificationError) as error:
            await verifier.verify(body, signature)
        assert error.value.reason == "invalid_signature"


async def test_keyset_rejects_string_key_id(signed_webhook):
    body, signed, keyset = signed_webhook
    keyset["key"][0]["keyId"] = str(keyset["key"][0]["keyId"])
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=keyset))
    ) as http:
        with pytest.raises(WebhookVerificationError) as error:
            await WebhookSignatureVerifier(KEYSET_URL, http).verify(
                body, base64.b64encode(signed).decode()
            )
    assert error.value.reason == "key_download_failed"


@pytest.mark.parametrize("keyset", [{}, {"key": "x"}, {"key": None}])
async def test_keyset_without_key_list_is_download_failure(signed_webhook, keyset):
    body, signed, _ = signed_webhook
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=keyset))
    ) as http:
        with pytest.raises(WebhookVerificationError) as error:
            await WebhookSignatureVerifier(KEYSET_URL, http).verify(
                body, base64.b64encode(signed).decode()
            )
    assert error.value.reason == "key_download_failed"


@pytest.mark.parametrize("keyset", [{}, {"key": "x"}, {"key": None}])
async def test_keyset_without_key_list_returns_retryable_503(signed_webhook, keyset):
    body, signed, _ = signed_webhook
    settings = Settings(_env_file=None, google_health_webhook_authorization="Bearer synthetic")
    app = FastAPI()
    app.state.settings = settings
    app.state.write_limits = WriteLimits(settings)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=keyset))
    ) as http:
        status = SimpleNamespace(record=AsyncMock())
        app.state.runtime = SimpleNamespace(
            webhook_verifier=WebhookSignatureVerifier(KEYSET_URL, http), webhook_status=status
        )
        app.include_router(webhooks.router)
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post(
                "/webhooks/google-health",
                content=body,
                headers={
                    "Authorization": "Bearer synthetic",
                    "GOOGLE-HEALTH-API-SIGNATURE": base64.b64encode(signed).decode(),
                },
            )
    assert response.status_code == 503
    status.record.assert_awaited_once_with("key_download_failed")


@pytest.mark.parametrize(
    ("length", "reason"),
    [(5, "malformed_signature"), (6, "invalid_signature")],
    ids=["prefix-only", "invalid-der"],
)
async def test_tink_signature_length_boundary(signed_webhook, length, reason):
    body, signed, keyset = signed_webhook
    signature = base64.b64encode(signed[:5] + b"\x00" * (length - 5)).decode()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=keyset))
    ) as http:
        with pytest.raises(WebhookVerificationError) as error:
            await WebhookSignatureVerifier(KEYSET_URL, http).verify(body, signature)
    assert error.value.reason == reason


@pytest.mark.parametrize(
    "serialized",
    [b"\x1a", _field(3, bytes(32)) + _field(4, bytes(32))],
    ids=["truncated-protobuf", "point-outside-curve"],
)
async def test_malformed_public_key_becomes_verification_error(signed_webhook, serialized):
    body, signed, keyset = signed_webhook
    keyset["key"][0]["keyData"]["value"] = base64.b64encode(serialized).decode()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=keyset))
    ) as http:
        with pytest.raises(WebhookVerificationError) as error:
            await WebhookSignatureVerifier(KEYSET_URL, http).verify(
                body, base64.b64encode(signed).decode()
            )
    assert error.value.reason == "invalid_signature"
    assert str(error.value) == "Webhook signature verification failed"


async def test_keyset_reused_until_expiry_then_downloaded(signed_webhook, monkeypatch):
    body, signed, keyset = signed_webhook
    now = 1000
    monkeypatch.setattr("app.google_health.webhook.time", SimpleNamespace(time=lambda: now))
    requests = []

    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(200, json=keyset)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        verifier = WebhookSignatureVerifier(KEYSET_URL, http, monotonic=lambda: now)
        signature = base64.b64encode(signed).decode()
        await verifier.verify(body, signature)
        now += 3599
        await verifier.verify(body, signature)
        assert requests == [KEYSET_URL]
        now += 1
        await verifier.verify(body, signature)
        assert requests == [KEYSET_URL, KEYSET_URL]


@pytest.mark.parametrize("target", [KEYSET_URL + "?redirected=1", "https://keys.example/keyset"])
async def test_keyset_redirect_never_requests_target(signed_webhook, target):
    body, signed, keyset = signed_webhook
    requests = []

    def handler(request):
        requests.append(str(request.url))
        if str(request.url) == KEYSET_URL:
            return httpx.Response(302, headers={"Location": target})
        return httpx.Response(200, json=keyset)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        try:
            with pytest.raises(WebhookVerificationError) as error:
                await WebhookSignatureVerifier(KEYSET_URL, http).verify(
                    body, base64.b64encode(signed).decode()
                )
            assert error.value.reason == "key_download_failed"
        finally:
            assert requests == [KEYSET_URL]


async def test_key_rotation_waits_for_refresh_interval(signed_webhook, receiver):
    body, signed, rotated = signed_webhook
    now = 0
    requests = []
    original = {"keyId": 41, "status": "ENABLED"}

    def handler(request):
        requests.append(request)
        keys = [original] + (rotated["key"] if now >= 10 else [])
        return httpx.Response(200, json={"key": keys})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        verifier = WebhookSignatureVerifier(KEYSET_URL, http, monotonic=lambda: now)
        await verifier._get_keyset()
        signature = base64.b64encode(signed).decode()
        now = 10
        with pytest.raises(WebhookVerificationError) as error:
            await verifier.verify(body, signature)
        assert error.value.reason == "key_download_failed"
        receiver.app.state.runtime.webhook_verifier = verifier
        assert (await receiver.post(body, signature=signature)).status_code == 503
        assert len(requests) == 1
        now = 61
        await verifier.verify(body, signature)
        assert len(requests) == 2


@pytest.mark.parametrize("mode", ["interval", "single-flight", "slow-single-flight"])
@pytest.mark.parametrize("rotated", [False, True], ids=["unknown", "new-key"])
@pytest.mark.parametrize("primed", [False, True], ids=["cold", "cached"])
async def test_keyset_refresh_burst_is_bounded(signed_webhook, mode, rotated, primed):
    body, signed, keyset = signed_webhook
    now = 0
    calls = 0

    async def handler(request):
        nonlocal calls, now
        calls += 1
        await asyncio.sleep(0)  # Let all concurrent requests reach the download lock.
        if mode == "slow-single-flight" and calls == 1 + int(primed):
            now += 61
        return httpx.Response(200, json=keyset if rotated and calls > int(primed) else {"key": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        verifier = WebhookSignatureVerifier(KEYSET_URL, http, monotonic=lambda: now)
        if primed:
            await verifier._get_keyset()
        now = 60

        async def verify():
            try:
                await verifier.verify(body, base64.b64encode(signed).decode())
            except WebhookVerificationError as exc:
                return exc.reason

        if mode != "interval":
            results = await asyncio.gather(*(verify() for _ in range(50)))
        else:
            results = [await verify() for _ in range(50)]
        assert calls == 1 + int(primed)
        assert all(
            result is None if rotated else result in {"invalid_signature", "key_download_failed"}
            for result in results
        )


@pytest.mark.parametrize("failure", [False, True], ids=["cold-cache", "failed-download"])
async def test_keyset_cold_burst_and_failure_retry_are_bounded(signed_webhook, failure):
    body, signed, keyset = signed_webhook
    now = 0
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return httpx.Response(503 if failure and now < 60 else 200, json=keyset)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        verifier = WebhookSignatureVerifier(KEYSET_URL, http, monotonic=lambda: now)
        signature = base64.b64encode(signed).decode()
        results = await asyncio.gather(
            *(verifier.verify(body, signature) for _ in range(50)), return_exceptions=True
        )
        assert calls == 1
        if failure:
            assert all(
                isinstance(result, WebhookVerificationError)
                and result.reason == "key_download_failed"
                for result in results
            )
            now = 59
            with pytest.raises(WebhookVerificationError):
                await verifier.verify(body, signature)
            assert calls == 1
            now = 60
            await verifier.verify(body, signature)
            assert calls == 2
        else:
            assert results == [None] * 50


class KeysetStream(httpx.AsyncByteStream):
    def __init__(self, body):
        self.body = body
        self.closed = False
        self.received = 0

    async def __aiter__(self):
        for offset in range(0, len(self.body), 4096):
            chunk = self.body[offset : offset + 4096]
            self.received += len(chunk)
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize(
    "encoding,size,accepted",
    [
        ("identity", 0, True),
        ("gzip", 0, True),
        ("identity", 65536, False),
        ("identity", 131072, False),
        ("gzip", 65536, False),
        ("br", 0, False),
    ],
)
async def test_keyset_body_budgets_and_encoding(signed_webhook, encoding, size, accepted):
    body, signed, keyset = signed_webhook
    raw = json.dumps({**keyset, "padding": " " * size}).encode()
    wire = gzip.compress(raw) if encoding == "gzip" else raw
    stream = KeysetStream(wire)

    def handler(request):
        assert request.headers["Accept-Encoding"] == "gzip, deflate"
        return httpx.Response(200, stream=stream, headers={"Content-Encoding": encoding})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        verifier = WebhookSignatureVerifier(KEYSET_URL, http)
        signature = base64.b64encode(signed).decode()
        if accepted:
            await verifier.verify(body, signature)
        else:
            with pytest.raises(WebhookVerificationError) as error:
                await verifier.verify(body, signature)
            assert error.value.reason == "key_download_failed"
        assert stream.closed
        if len(wire) > 65536:
            assert stream.received <= 65536 + 4096


@pytest.mark.parametrize(
    "damage", ["split", "padding", "oversized", "truncated", "checksum", "junk"]
)
async def test_multimember_keyset_is_validated_before_cache(signed_webhook, damage):
    body, signed, keyset = signed_webhook
    raw = json.dumps(keyset).encode()
    tail = b" " * 65536 if damage == "oversized" else b" "
    wire = (
        compressed(raw, "gzip")
        + b"\0\0"
        + compressed(
            tail, "gzip", damage if damage in {"truncated", "checksum", "junk"} else "valid"
        )
        + b"\0"
    )
    if damage == "split":
        wire = compressed(raw[:20], "gzip") + b"\0" + compressed(raw[20:], "gzip")
    accepted, now, calls, streams = damage in {"split", "padding"}, 0, [], []

    async def handler(request):
        calls.append(request)
        await asyncio.sleep(0)  # All deliveries share the in-progress download.
        stream = KeysetStream(wire)
        streams.append(stream)
        return httpx.Response(200, stream=stream, headers={"content-encoding": "gzip"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        verifier = WebhookSignatureVerifier(KEYSET_URL, http, monotonic=lambda: now)
        signature = base64.b64encode(signed).decode()
        results = await asyncio.gather(
            *(verifier.verify(body, signature) for _ in range(12)), return_exceptions=True
        )
        assert len(calls) == 1 and all(stream.closed for stream in streams)
        if accepted:
            assert results == [None] * 12 and verifier._cache.value == keyset
            await verifier.verify(body, signature)
            assert len(calls) == 1
        else:
            assert verifier._cache is None
            assert all(
                isinstance(r, WebhookVerificationError) and r.reason == "key_download_failed"
                for r in results
            )
            now = 59
            with pytest.raises(WebhookVerificationError):
                await verifier.verify(body, signature)
            assert len(calls) == 1
            now, wire = 60, compressed(raw, "gzip")
            await verifier.verify(body, signature)
            assert len(calls) == 2 and verifier._cache.value == keyset


@pytest.mark.parametrize("status", [200, 503])
@pytest.mark.parametrize("cancel", [False, True])
async def test_keyset_close_settles_before_unlock_and_recovers(signed_webhook, status, cancel):
    body, signed, keyset = signed_webhook
    wire, now, calls = json.dumps(keyset).encode(), 0, []
    stream = ClosingStream(wire, expire=not cancel, close_error=TimeoutError)
    stream.cleanup.clear()

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status if len(calls) == 1 else 200,
            stream=stream if len(calls) == 1 else httpx.ByteStream(wire),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        verifier = WebhookSignatureVerifier(KEYSET_URL, http, monotonic=lambda: now)
        signature = base64.b64encode(signed).decode()
        task = asyncio.create_task(verifier.verify(body, signature))
        waiter = None
        try:
            if cancel:
                await repeated_cancel(task, stream)
            else:
                await stream.cancelling.wait()
            waiter = asyncio.create_task(verifier.verify(body, signature))
            await asyncio.sleep(0)
            assert verifier._refresh_lock.locked() and verifier._cache is None
            assert not task.done() and not waiter.done() and len(calls) == 1
            stream.cleanup.set()
            failure = asyncio.CancelledError if cancel else WebhookVerificationError
            with pytest.raises(failure) as err:
                await task
            safe_chain(err.value)
            with pytest.raises(WebhookVerificationError) as err:
                await waiter
            assert err.value.reason == "key_download_failed"
            stream.assert_settled()
            assert not verifier._refresh_lock.locked() and verifier._cache is None
            now = 59
            with pytest.raises(WebhookVerificationError):
                await verifier.verify(body, signature)
            assert len(calls) == 1
            now = 60
            await verifier.verify(body, signature)
            assert len(calls) == 2 and verifier._cache.value == keyset
        finally:
            await dispose(task, stream)
            if waiter is not None:
                await dispose(waiter, stream)
