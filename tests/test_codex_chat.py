import asyncio
import json
import logging
import ssl
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from pydantic import ValidationError
from sqlalchemy import func, select
from support.chat import RPC, Browser, take
from support.codex_bridge import attested_thread, verified_bridge_config
from websockets.exceptions import ConnectionClosed

from app.api import codex
from app.models.state import RawHealthData
from app.services.umay import chat_relay, codex_chat, codex_chat_thread
from app.services.umay.chat_context import SUMMARY_LABEL
from app.services.umay.chat_socket import ChatDisconnected as WebSocketDisconnect
from app.services.umay.codex_bridge_tls import secure_bridge_url
from app.services.umay.codex_chat import BridgeUnverified, CodexRPC
from app.services.umay.codex_health import ArchiveQuery, read_health


def raw(identifier, timestamp, **overrides):
    values = {
        "data_type": "heart-rate",
        "google_data_point_id": identifier,
        "source_start_time": datetime.fromisoformat(timestamp).replace(tzinfo=UTC),
        "raw_json": {"heartRate": {"bpm": 70}},
        "payload_hash": "test",
        "first_seen_at": datetime.now(UTC),
        "last_seen_at": datetime.now(UTC),
    }
    values.update(overrides)
    return RawHealthData(**values)


async def test_archive_inventory_pagination_and_read_only(sessions, monkeypatch):
    async with sessions() as session:
        session.add_all(
            [
                raw("before", "2026-09-28T21:59:00"),
                raw("first", "2026-09-28T22:00:00"),
                raw("second", "2026-09-29T21:59:00"),
                raw("after", "2026-09-29T22:00:00"),
                raw("quarantine", "2026-09-29T12:00:00", is_quarantined=True),
                raw("deleted", "2026-09-29T12:00:00", source_deleted_at=datetime.now(UTC)),
            ]
        )
        await session.commit()
    inventory = await read_health(sessions, {"action": "inventory"})
    assert inventory["streams"][0]["count"] == 4
    query = {
        "action": "records",
        "data_type": "heart-rate",
        "start_date": "2026-09-29",
        "end_date": "2026-09-29",
        "timezone": "Europe/Paris",
        "limit": 1,
    }
    first = await read_health(sessions, query)
    assert first["records"][0]["start"].startswith("2026-09-28T22:00")
    assert first["next_offset"] == 1
    second = await read_health(sessions, dict(query, offset=first["next_offset"]))
    assert second["records"][0]["start"].startswith("2026-09-29T21:59")
    assert second["next_offset"] is None
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(RawHealthData)) == 6


async def test_daily_tool_reuses_real_read_model(sessions, monkeypatch):
    result = await read_health(
        sessions,
        {
            "action": "daily",
            "start_date": "2026-09-28",
            "end_date": "2026-09-29",
            "timezone": "UTC",
        },
    )
    assert len(result["days"]) == 2
    assert result["days"][0]["steps"] is None
    assert "sleep_method" in result


@pytest.mark.parametrize(
    "arguments",
    [
        {"action": "delete"},
        {"action": "inventory", "sql": "DROP TABLE raw_health_data"},
        {"action": "daily", "start_date": "2020-01-01", "end_date": "2026-09-29"},
        {"action": "daily", "start_date": "2099-01-01", "end_date": "2099-01-01"},
        {"action": "records", "start_date": "2026-09-01", "end_date": "2026-09-29"},
        {"action": "inventory", "limit": 201},
        {
            "action": "records",
            "data_type": "heart-rate",
            "start_date": "2026-09-29",
            "end_date": "2026-09-28",
        },
    ],
)
def test_archive_query_rejects_unbounded_or_write_requests(arguments):
    with pytest.raises(ValidationError):
        ArchiveQuery.model_validate(arguments)


def test_same_origin_protects_local_subscription():
    assert codex.same_origin("http://localhost:8000", "localhost:8000")
    assert not codex.same_origin("https://attacker.example", "localhost:8000")
    assert not codex.same_origin("null", "localhost:8000")
    assert not codex.same_origin(None, "localhost:8000")
    assert not codex.same_origin("http://[invalid", "localhost:8000")


async def test_new_thread_pins_requested_model_and_is_ephemeral(tmp_path, monkeypatch):
    class Connection:
        def __init__(self):
            self.sent = []
            self.responses = asyncio.Queue()

        async def send(self, text):
            request = json.loads(text)
            self.sent.append(request)
            if "id" not in request:
                return
            result = {
                "initialize": {},
                "account/read": {"account": {"type": "chatgpt"}},
                "config/read": verified_bridge_config(),
                "thread/start": attested_thread("new-site-thread"),
            }[request["method"]]
            await self.responses.put(json.dumps({"id": request["id"], "result": result}))

        async def recv(self):
            return await self.responses.get()

    connection = Connection()
    thread, model = await CodexRPC(connection).start()
    assert (thread, model) == ("new-site-thread", "gpt-6.1-sol")
    params = connection.sent[-1]["params"]
    assert params["ephemeral"] is True
    assert params["model"] == "gpt-6.1-sol"
    assert params["allowProviderModelFallback"] is False
    assert params["config"]["model_reasoning_effort"] == "high"
    assert params["sandbox"] == "read-only"
    assert params["dynamicTools"][0]["name"] == "health_archive"
    assert "# Umay" in params["developerInstructions"]
    assert "Örnek diyaloglar üslup örneğidir" in params["developerInstructions"]
    assert params["baseInstructions"] == codex_chat.INSTRUCTIONS
    assert "health_archive" in params["baseInstructions"]

    # Edits apply to the next site thread; the running thread's snapshot stays fixed.
    soul = tmp_path / "soul.md"
    soul.write_text("# Umay\nYeni kişilik tercihi: daha şiirsel.", encoding="utf-8")
    monkeypatch.setattr(codex_chat_thread, "SOUL_PATH", soul)
    await CodexRPC(connection).start()
    updated = connection.sent[-1]["params"]
    assert "Yeni kişilik tercihi" in updated["developerInstructions"]
    assert "Yeni kişilik tercihi" not in params["developerInstructions"]
    assert updated["baseInstructions"] == params["baseInstructions"]
    assert updated["dynamicTools"] == params["dynamicTools"]


def test_empty_umay_persona_is_rejected(tmp_path, monkeypatch):
    soul = tmp_path / "soul.md"
    soul.write_text(" \n", encoding="utf-8")
    monkeypatch.setattr(codex_chat_thread, "SOUL_PATH", soul)
    with pytest.raises(ValueError, match="kişilik dosyası boş"):
        codex_chat.load_umay_persona()


async def test_relay_tools_streaming_followup_and_cancel(monkeypatch):
    browser, rpc = Browser(), RPC()

    async def health(_sessions, arguments):
        assert arguments == {"action": "inventory"}
        return {"streams": []}

    monkeypatch.setattr(chat_relay, "read_health", health)
    task = asyncio.create_task(chat_relay.relay(browser, rpc, "site-thread"))
    try:
        await browser.incoming.put({"text": "Uykumu incele. Kısa ve düz anlat."})
        start = await take(rpc.outgoing)
        assert start["params"]["threadId"] == "site-thread"
        assert (await take(browser.outgoing))["type"] == "working"
        await rpc.incoming.put({"id": start["id"], "result": {"turn": {"id": "turn-1"}}})
        await rpc.incoming.put(
            {
                "id": "tool-1",
                "method": "item/tool/call",
                "params": {
                    "threadId": "site-thread",
                    "tool": "health_archive",
                    "arguments": {"action": "inventory"},
                },
            }
        )
        assert (await take(browser.outgoing))["type"] == "working"
        reply = await take(rpc.outgoing)
        assert reply["result"]["success"] is True
        assert json.loads(reply["result"]["contentItems"][0]["text"]) == {"streams": []}
        await rpc.incoming.put(
            {
                "method": "item/agentMessage/delta",
                "params": {
                    "threadId": "other-thread",
                    "itemId": "other",
                    "delta": "PRIVATE",
                },
            }
        )
        await rpc.incoming.put(
            {
                "method": "item/agentMessage/delta",
                "params": {
                    "threadId": "site-thread",
                    "itemId": "answer",
                    "delta": "Veri yok.",
                },
            }
        )
        assert (await take(browser.outgoing))["text"] == "Veri yok."
        await rpc.incoming.put(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "site-thread",
                    "turn": {"status": "completed"},
                },
            }
        )
        assert (await take(browser.outgoing))["status"] == "completed"
        await browser.incoming.put({"text": "Peki geçen ay?"})
        followup = await take(rpc.outgoing)
        assert followup["params"]["threadId"] == "site-thread"
        summary = json.loads(followup["params"]["input"][1]["text"].removeprefix(SUMMARY_LABEL))
        assert summary["topic_hints"][0]["quote"] == "Uykumu incele."
        assert summary["explicit_preferences"]["length"]["quote"] == "Kısa ve düz anlat."
        assert summary["recent_requests"][0]["outcome"] == "response_received"
        await take(browser.outgoing)
        await browser.incoming.put({"type": "cancel"})
        assert (await take(browser.outgoing))["text"] == "Yanıt durduruluyor…"
        # Cancellation arriving before turn/start's reply must be retained.
        await rpc.incoming.put({"id": followup["id"], "result": {"turn": {"id": "turn-2"}}})
        cancel = await take(rpc.outgoing)
        assert cancel["method"] == "turn/interrupt"
        assert cancel["params"]["turnId"] == "turn-2"
    finally:
        await browser.incoming.put(WebSocketDisconnect())
        with pytest.raises(WebSocketDisconnect):
            await asyncio.wait_for(task, 30)


async def test_relay_guards_input_tools_errors_and_closing_interrupt(monkeypatch):
    browser, rpc = Browser(), RPC()

    async def health(_sessions, arguments):
        raise RuntimeError("database down")

    monkeypatch.setattr(chat_relay, "read_health", health)
    task = asyncio.create_task(chat_relay.relay(browser, rpc, "site-thread"))
    try:
        # Idle cancel is a no-op; the next reply belongs to the non-object message.
        await browser.incoming.put({"type": "cancel"})
        await browser.incoming.put(["not", "an", "object"])
        assert await take(browser.outgoing) == {"type": "error", "text": "Bir soru yaz."}
        for invalid in ({"text": "   "}, {"text": "Uyku", "timezone": "Mars/Olympus"}):
            await browser.incoming.put(invalid)
            assert (await take(browser.outgoing))["text"].startswith("Geçerli bir soru yaz")
        assert rpc.outgoing.empty()

        # A rejected turn/start ends the turn so the next question is accepted.
        await browser.incoming.put({"text": "Uykumu incele"})
        first = await take(rpc.outgoing)
        await take(browser.outgoing)
        await browser.incoming.put({"text": "İkinci soru"})
        assert (await take(browser.outgoing))["type"] == "notice"
        await rpc.incoming.put({"id": first["id"], "error": {"message": "model busy"}})
        assert await take(browser.outgoing) == {"type": "error", "text": "model busy"}

        await browser.incoming.put({"text": "Tekrar dene"})
        second = await take(rpc.outgoing)
        await take(browser.outgoing)
        await rpc.incoming.put({"id": second["id"], "result": {"turn": {"id": "turn-2"}}})
        # Only this thread's tool calls are served; everything else is rejected.
        for foreign in (
            {"id": "x1", "method": "item/commandExecution/requestApproval", "params": {}},
            {
                "id": "x2",
                "method": "item/tool/call",
                "params": {"threadId": "other-thread", "tool": "health_archive"},
            },
        ):
            await rpc.incoming.put(foreign)
            assert await take(rpc.outgoing) == {"id": foreign["id"], "error": "unsupported"}
        for tool_id, params, error in (
            ("t1", {"tool": "shell", "arguments": {}}, "Sorgu geçersiz"),
            ("t2", {"tool": "health_archive", "arguments": "{not json"}, "Sorgu geçersiz"),
            ("t3", {"tool": "health_archive", "arguments": {}}, "Sağlık arşivi okunamadı"),
        ):
            await rpc.incoming.put(
                {
                    "id": tool_id,
                    "method": "item/tool/call",
                    "params": {"threadId": "site-thread", **params},
                }
            )
            assert (await take(browser.outgoing))["type"] == "working"
            reply = await take(rpc.outgoing)
            assert reply["id"] == tool_id
            assert reply["result"]["success"] is False
            assert json.loads(reply["result"]["contentItems"][0]["text"])["error"].startswith(error)
        await rpc.incoming.put(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "site-thread",
                    "item": {"type": "agentMessage", "id": "answer", "text": "Tamam."},
                },
            }
        )
        assert await take(browser.outgoing) == {"type": "message", "id": "answer", "text": "Tamam."}

        # With the turn id known, cancel interrupts at once.
        await browser.incoming.put({"type": "cancel"})
        interrupt = await take(rpc.outgoing)
        assert interrupt["method"] == "turn/interrupt"
        assert interrupt["params"] == {"threadId": "site-thread", "turnId": "turn-2"}
        assert (await take(browser.outgoing))["text"] == "Yanıt durduruluyor…"
        await rpc.incoming.put(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "site-thread",
                    "turn": {"status": "failed", "error": {"message": "quota"}},
                },
            }
        )
        assert await take(browser.outgoing) == {"type": "error", "text": "quota"}
        assert await take(browser.outgoing) == {"type": "done", "status": "failed"}

        await browser.incoming.put({"text": "Son soru"})
        third = await take(rpc.outgoing)
        await take(browser.outgoing)
        await rpc.incoming.put({"id": third["id"], "result": {"turn": {"id": "turn-3"}}})
        await rpc.incoming.put({"id": "barrier", "method": "unsupported", "params": {}})
        assert (await take(rpc.outgoing))["id"] == "barrier"
    finally:
        await browser.incoming.put(WebSocketDisconnect())
        with pytest.raises(WebSocketDisconnect):
            await asyncio.wait_for(task, 30)
    # Leaving mid-turn interrupts the running turn.
    closing = await take(rpc.outgoing)
    assert closing["method"] == "turn/interrupt"
    assert closing["params"]["turnId"] == "turn-3"


async def test_relay_times_out_an_idle_chat(monkeypatch):
    browser, rpc = Browser(), RPC()
    monkeypatch.setattr(chat_relay, "IDLE_TIMEOUT_SECONDS", 0.05)
    await asyncio.wait_for(chat_relay.relay(browser, rpc, "site-thread"), 30)
    message = await take(browser.outgoing)
    assert message["type"] == "error"
    assert "zaman aşımına" in message["text"]
    assert rpc.outgoing.empty()


async def test_health_tool_propagates_cancellation_and_cleans_up(monkeypatch):
    entered, cleaned = asyncio.Event(), asyncio.Event()

    async def health(_sessions, _arguments):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    monkeypatch.setattr(chat_relay, "read_health", health)
    task = asyncio.create_task(
        chat_relay.run_health_tool({"tool": "health_archive", "arguments": {}})
    )
    await asyncio.wait_for(entered.wait(), 30)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned.is_set()


def test_unavailable_bridge_warns_once_per_outage(monkeypatch, caplog):
    monkeypatch.setattr(codex, "_bridge_unavailable_logged", False)
    caplog.set_level(logging.DEBUG, logger=codex.logger.name)

    for _ in range(5):
        codex._log_bridge_unavailable()
    codex._log_bridge_available()
    codex._log_bridge_unavailable()

    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    debug = [record for record in caplog.records if record.levelno == logging.DEBUG]
    assert len(warnings) == 2
    assert len(debug) == 4


class SiteSocket:
    def __init__(self, token="synthetic", origin="http://localhost:8000"):
        self.app = FastAPI()
        self.app.include_router(codex.router)
        self.app.state.settings = SimpleNamespace(
            app_ui_token="", codex_bridge_token=token, codex_bridge_url="ws://bridge.invalid:8766"
        )
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()
        self.scope = {
            "type": "websocket",
            "path": "/api/codex/chat",
            "scheme": "ws",
            "query_string": b"",
            "headers": [(b"host", b"localhost:8000"), (b"origin", origin.encode())],
        }

    async def start(self):
        await self.incoming.put({"type": "websocket.connect"})
        self.task = asyncio.create_task(self.app(self.scope, self.incoming.get, self.outgoing.put))
        return await self.frame()

    async def frame(self):
        return await asyncio.wait_for(self.outgoing.get(), 30)

    async def message(self):
        frame = await self.frame()
        assert frame["type"] == "websocket.send"
        return json.loads(frame["text"])

    async def finish(self):
        assert (await self.frame())["type"] == "websocket.close"
        await asyncio.wait_for(self.task, 30)

    async def leave(self):
        await self.incoming.put({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(self.task, 30)


class SyntheticBridge:
    def __init__(self, failure=None, stall=False):
        self.failure = failure
        self.stall = stall
        self.responses = asyncio.Queue()
        self.requests = []
        self.closed = False

    async def __aenter__(self):
        if self.failure:
            raise self.failure
        return self

    async def __aexit__(self, *_args):
        self.closed = True

    async def send(self, text):
        request = json.loads(text)
        self.requests.append(request)
        if self.stall or "id" not in request:
            return
        result = {
            "initialize": {},
            "account/read": {"account": {"type": "chatgpt"}},
            "config/read": verified_bridge_config(),
            "thread/start": attested_thread("synthetic-thread"),
        }[request["method"]]
        await self.responses.put(json.dumps({"id": request["id"], "result": result}))

    async def recv(self):
        response = await self.responses.get()
        if response is None:
            raise ConnectionClosed(None, None)
        return response


@pytest.mark.parametrize(
    ("token", "failure", "expected"),
    [
        (None, None, "yapılandırılmamış"),
        ("   ", None, "yapılandırılmamış"),
        ("synthetic", ConnectionRefusedError("private token"), "ulaşılamıyor"),
        ("synthetic", TimeoutError("private token"), "bağlanırken zaman aşımı"),
        ("synthetic", RuntimeError("Codex private token"), "ayarlarını ve oturumu"),
        ("synthetic", BridgeUnverified("bridge feature not disabled"), "köprüsünü yeniden"),
        ("synthetic", "stall", "hazırlanması uzun sürdü"),
    ],
)
async def test_site_connection_errors_are_safe_and_close_resources(
    monkeypatch, token, failure, expected
):
    transport = SyntheticBridge(
        failure=failure if failure != "stall" else None, stall=failure == "stall"
    )
    options = {}

    def connect(url, **kwargs):
        options.update(url=url, **kwargs)
        return transport

    monkeypatch.setattr(codex, "connect", connect)
    monkeypatch.setattr(codex, "START_TIMEOUT_SECONDS", 0.02)
    site = SiteSocket(token=token)
    assert (await site.start())["type"] == "websocket.accept"
    message = await site.message()
    assert message["type"] == "error" and "Umay" in message["text"]
    assert expected in message["text"]
    assert all(word not in message["text"].lower() for word in ("codex", "token", "private"))
    await site.finish()
    if not (token or "").strip():
        assert not options  # configuration failures must not attempt a bridge connection
    else:
        assert options["url"] == secure_bridge_url(site.app.state.settings.codex_bridge_url)
        assert isinstance(options["ssl"], ssl.SSLContext)
        assert options["ssl"].verify_mode is ssl.CERT_REQUIRED
        assert options["additional_headers"] == {"Authorization": "Bearer synthetic"}
        assert options["open_timeout"] == codex.CONNECT_TIMEOUT_SECONDS
        if failure == "stall":
            assert transport.closed


async def test_site_socket_disconnect_and_recovery_close_both_transports(monkeypatch):
    transports = [SyntheticBridge(), SyntheticBridge()]
    pending = iter(transports)
    monkeypatch.setattr(codex, "connect", lambda *args, **kwargs: next(pending))
    first = SiteSocket()
    await first.start()
    assert (await first.message())["type"] == "ready"
    await transports[0].responses.put(None)
    assert await first.message() == {
        "type": "error",
        "text": "Umay ile bağlantı kesildi. Bağlantı yeniden denenecek.",
    }
    await first.finish()
    second = SiteSocket()
    await second.start()
    assert (await second.message())["type"] == "ready"
    await second.leave()
    assert all(t.closed and t.requests[-1]["params"]["ephemeral"] for t in transports)


async def test_site_chat_uses_the_injected_archive_factory(sessions, monkeypatch):
    async with sessions.begin() as session:
        session.add(raw("injected", "2026-09-29T12:00:00"))
    transport = SyntheticBridge()
    monkeypatch.setattr(codex, "connect", lambda *args, **kwargs: transport)
    site = SiteSocket()
    site.app.dependency_overrides[codex.chat_sessions] = lambda: sessions

    async def relay(socket, rpc, thread_id, *, sessions, **kwargs):
        result = await read_health(sessions, {"action": "inventory"})
        await socket.send_json({"type": "inventory", "count": result["streams"][0]["count"]})

    monkeypatch.setattr(codex, "relay", relay)
    assert (await site.start())["type"] == "websocket.accept"
    assert (await site.message())["type"] == "ready"
    assert await site.message() == {"type": "inventory", "count": 1}
    await site.finish()
    assert transport.closed


def test_turn_context_carries_real_local_date(monkeypatch):
    from datetime import UTC

    fixed = datetime(2026, 10, 24, 20, 5, tzinfo=UTC)
    text = chat_relay.turn_context("Europe/Brussels", now=fixed)
    assert text.startswith("Kullanıcının saat dilimi: Europe/Brussels.")
    assert "24 Ekim 2026 Cumartesi, saat 22:05" in text
    assert text.endswith("\n\n")


def test_persona_has_no_scripted_opening_and_high_effort():
    persona = codex_chat.load_umay_persona()
    assert "Kişilik sürümü: 7" in persona
    assert "Bu yeni sohbetin açılış yaklaşımı" not in persona
    assert not hasattr(codex_chat, "GREETING_APPROACHES")
    assert codex_chat.EFFORT == "high"
