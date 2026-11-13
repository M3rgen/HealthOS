"""Webhook test helpers: notifications, the test client and receivers, synthetic data only."""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from sync_ownership_records import service

from app.api import webhooks
from app.core.auth import SessionStore, UITokenMiddleware
from app.core.config import Settings
from app.core.host_validation import HostValidationMiddleware
from app.core.request_limits import WriteLimits
from app.google_health.client import GoogleHealthClient
from app.google_health.data_types import REGISTRY
from app.google_health.webhook import WebhookSignatureVerifier
from app.models.state import OAuthToken
from app.services.synchronization.webhook_status import (
    WebhookStatusService,
)
from app.sync import webhook_processing
from app.sync.webhook_processing import WebhookBatchProcessor
from support.google_client import Tokens


class Verifier:
    def __init__(self):
        self.bodies = []

    async def verify(self, body, signature):
        assert signature == "signed"
        self.bodies.append(body)


class Incremental:
    specs = {"steps": REGISTRY["steps"], "heart-rate": REGISTRY["heart-rate"]}

    def __init__(self, fail_once=()):
        self.calls = []
        self.fail_once = set(fail_once)

    async def run(self, data_type, start=None, end=None, *, check_owner=None):
        self.calls.append((data_type, start, end))
        if data_type in self.fail_once:
            self.fail_once.remove(data_type)
            raise RuntimeError(f"temporary {data_type} failure")


def notification(data_type="steps", user="user-1", minute=0):
    return {
        "data": {
            "dataType": data_type,
            "healthUserId": user,
            "operation": "UPSERT",
            "intervals": [
                {
                    "physicalTimeInterval": {
                        "startTime": f"2026-08-17T10:{minute:02d}:00Z",
                        "endTime": f"2026-08-17T10:{minute + 1:02d}:00Z",
                    }
                }
            ],
        }
    }


async def make_client(sessions, incremental=None):
    async with sessions.begin() as session:
        session.add(
            OAuthToken(
                subject="me",
                encrypted_refresh_token="encrypted",
                health_user_id="user-1",
            )
        )
    verifier = Verifier()
    incremental = incremental or Incremental()
    processor = WebhookBatchProcessor(sessions, incremental, set(incremental.specs))
    tasks = []

    def spawn(coroutine):
        task = asyncio.create_task(coroutine)
        tasks.append(task)
        return task

    application = FastAPI()
    application.state.settings = Settings(google_health_webhook_authorization="Bearer local-test")
    application.state.runtime = SimpleNamespace(
        webhook_verifier=verifier,
        webhook_processor=processor,
        spawn=spawn,
        webhook_status=WebhookStatusService(application.state.settings, sessions),
    )
    application.state.write_limits = WriteLimits(application.state.settings)
    application.include_router(webhooks.router)
    transport = httpx.ASGITransport(app=application)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    return client, verifier, incremental, processor, tasks


async def finish(tasks):
    await asyncio.gather(*tasks)
    tasks.clear()


START = datetime(2026, 10, 1, tzinfo=UTC)


def configured():
    return Settings(
        _env_file=None,
        google_health_webhook_endpoint_uri="https://hooks.example/webhooks/google-health",
        google_health_webhook_authorization="Bearer synthetic-test",
    )


@pytest.fixture
async def receiver(sessions, monkeypatch):
    current = SimpleNamespace(now=START, key_requests=0, key_response="ok")

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ARG003
            return current.now

    monkeypatch.setattr(webhook_processing, "datetime", Frozen)
    settings = configured()
    status = WebhookStatusService(settings, sessions, lambda: current.now)
    await status.start()
    key = ec.generate_private_key(ec.SECP256R1())
    numbers = key.public_key().public_numbers()
    serialized = b"\x08\x00\x1a\x20" + numbers.x.to_bytes(32) + b"\x22\x20" + numbers.y.to_bytes(32)
    keyset = {
        "key": [
            {
                "keyId": 42,
                "status": "ENABLED",
                "keyData": {"value": base64.b64encode(serialized).decode()},
            }
        ]
    }

    def key_server(request):
        current.key_requests += 1
        if current.key_response == "timeout":
            raise httpx.ReadTimeout("synthetic private upstream error", request=request)
        if current.key_response == "503":
            return httpx.Response(503, text="synthetic private upstream error")
        if current.key_response == "malformed":
            return httpx.Response(200, json={"key": ["bad"]})
        if current.key_response == "json":
            return httpx.Response(200, text="not json")
        return httpx.Response(200, json=keyset)

    key_http = httpx.AsyncClient(transport=httpx.MockTransport(key_server))
    verifier = WebhookSignatureVerifier(settings.google_health_webhook_public_key_url, key_http)
    incremental = Incremental()
    processor = WebhookBatchProcessor(sessions, incremental, set(incremental.specs), status)
    async with sessions.begin() as session:
        session.add(
            OAuthToken(subject="me", encrypted_refresh_token="synthetic", health_user_id="user-1")
        )
    tasks = []

    def spawn(coroutine):
        tasks.append(asyncio.create_task(coroutine))

    async def finish():
        try:
            await asyncio.gather(*tasks)
        finally:
            tasks.clear()

    def sign(body):
        signed = b"\x01\x00\x00\x00\x2a" + key.sign(body, ec.ECDSA(hashes.SHA256()))
        return base64.b64encode(signed).decode()

    app = FastAPI()
    app.state.settings = settings
    app.state.runtime = SimpleNamespace(
        webhook_verifier=verifier, webhook_processor=processor, webhook_status=status, spawn=spawn
    )
    app.add_middleware(UITokenMiddleware, token="synthetic-ui", sessions=SessionStore())
    app.add_middleware(
        HostValidationMiddleware, webhook_endpoint_uri=settings.google_health_webhook_endpoint_uri
    )
    app.state.write_limits = WriteLimits(app.state.settings)
    app.include_router(webhooks.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:

        async def post(body=None, signature=True, authorization="Bearer synthetic-test"):
            body = body if body is not None else json.dumps(notification()).encode()
            headers = {}
            if authorization is not None:
                headers["Authorization"] = authorization
            if signature is not None:
                headers["GOOGLE-HEALTH-API-SIGNATURE"] = (
                    sign(body) if signature is True else signature
                )
            return await client.post("/webhooks/google-health", content=body, headers=headers)

        yield SimpleNamespace(
            client=client,
            post=post,
            sign=sign,
            status=status,
            current=current,
            processor=processor,
            finish=finish,
            incremental=incremental,
            app=app,
        )
        await finish()
    await key_http.aclose()


def bounded(kind, start, end):
    item = notification(kind)
    item["data"]["intervals"][0]["physicalTimeInterval"] = {
        "startTime": start,
        "endTime": end,
    }
    return item


@pytest.fixture
async def fetching_receiver(receiver, sessions):
    """Exercise real incremental, receipts, Google filters and deletion scopes."""
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"dataPoints": [], "rollupDataPoints": []})
        )
    ) as http:
        client = GoogleHealthClient(
            Settings(_env_file=None, google_api_min_request_interval_seconds=0), Tokens(), http
        )
        incremental = service(sessions, client)
        incremental.calls = []
        original = client.list_data_points

        async def record(spec, start, end, page_token=None):
            incremental.calls.append((spec.name, start, end))
            return await original(spec, start, end, page_token)

        client.list_data_points = record
        receiver.incremental = receiver.processor.incremental = incremental
        yield receiver
