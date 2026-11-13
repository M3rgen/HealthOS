"""Protocol boundaries must reject lookalike requests before reading the archive."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from support.chat import RPC, Browser, tool_request
from support.webhooks import finish, make_client, notification

from app.models.state import WebhookEvent, WebhookNotification
from app.services.umay import chat_relay


@pytest.mark.parametrize(
    "method", ["item/tool/requestUserInput", "item/commandExecution/requestApproval"]
)
async def test_only_tool_call_method_can_read_health_archive(monkeypatch, method):
    browser, rpc = Browser(), RPC()
    read = AsyncMock(return_value={"streams": []})
    monkeypatch.setattr(chat_relay, "read_health", read)
    chat = chat_relay.ChatRelay(browser, rpc, "site-thread")
    await chat.on_browser({"text": "Sentetik soru"})
    await chat.on_codex({"id": 1, "result": {"turn": {"id": "turn-1"}}})
    await rpc.outgoing.get()  # turn/start
    try:
        request = tool_request("lookalike")
        request["method"] = method  # all other fields are valid, including the tool name
        await chat.on_codex(request)
        await asyncio.sleep(0)  # an incorrectly scheduled read would now run
        read.assert_not_awaited()
        assert await rpc.outgoing.get() == {"id": "lookalike", "error": "unsupported"}
        assert not chat.tools.tasks
        # The same connection must still deliver a real tool call successfully.
        await chat.on_codex(tool_request("allowed"))
        task = next(iter(chat.tools.tasks))
        await task
        assert await chat.on_tool_result(task)
        read.assert_awaited_once_with(None, {"action": "inventory"})
        reply = await rpc.outgoing.get()
        assert reply["id"] == "allowed" and reply["result"]["success"]
        assert json.loads(reply["result"]["contentItems"][0]["text"]) == {"streams": []}
    finally:
        await chat.tools.close()


@pytest.mark.parametrize("extra", [{"data": {}}, {"unexpected": True}])
async def test_only_exact_verification_payload_bypasses_signature(sessions, extra):
    client, verifier, incremental, _, tasks = await make_client(sessions)
    headers = {"Authorization": "Bearer local-test"}
    payload = {"type": "verification", **extra}
    async with client:
        exact = await client.post(
            "/webhooks/google-health", json={"type": "verification"}, headers=headers
        )
        refused = await client.post("/webhooks/google-health", json=payload, headers=headers)
        assert exact.status_code == 201
        assert refused.status_code == 401
        assert verifier.bodies == []
        signed_body = json.dumps(payload).encode()
        signed = await client.post(
            "/webhooks/google-health",
            content=signed_body,
            headers={**headers, "GOOGLE-HEALTH-API-SIGNATURE": "signed"},
        )
        await finish(tasks)
    assert signed.status_code == 204
    assert verifier.bodies == [signed_body]
    assert not incremental.calls
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(WebhookEvent)) == 1
        batch = await session.scalar(select(WebhookEvent))
        assert batch.raw_body == signed_body and batch.raw_json == payload


async def test_running_notification_is_not_claimed_again_before_recovery(sessions):
    client, _, incremental, processor, _ = await make_client(sessions)
    async with client:
        payload = notification()
        batch = await processor.ingest_verified(json.dumps(payload).encode(), payload)
        async with sessions.begin() as session:
            element = await session.scalar(select(WebhookNotification))
            element.status = "running"  # previous process released ownership without finalizing
            element.attempt_count = 1
            digest = element.element_hash
        result = await processor.process_batch(batch.batch_hash)
        assert result.completed_elements == 0
        assert incremental.calls == []
        async with sessions() as session:
            element = await session.get(WebhookNotification, digest)
            assert element.status == "running" and element.attempt_count == 1
            assert element.processed_at is None
        # Recovery's failed state can be claimed, and is processed exactly once.
        async with sessions.begin() as session:
            element = await session.get(WebhookNotification, digest)
            element.status = "failed"
        assert (await processor.process_batch(batch.batch_hash)).completed_elements == 1
        assert len(incremental.calls) == 1
        async with sessions() as session:
            element = await session.get(WebhookNotification, digest)
            assert element.status == "completed" and element.attempt_count == 2
