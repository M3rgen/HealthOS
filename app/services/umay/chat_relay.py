"""Ephemeral chat turns, health tools and bidirectional socket relay."""

import asyncio
import json
import logging
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from websockets.exceptions import ConnectionClosed

from app.db.contracts import SessionFactory

from .chat_context import ConversationWindow
from .chat_socket import ChatDisconnected, ChatSocket
from .chat_tools import HealthQueries, ToolCall, ToolResult
from .codex_chat import EFFORT, MODEL, CodexRPC, JsonObject
from .codex_health import read_health
from .health_transfer import HealthTransfer, describe_result, neutral_receipt

logger = logging.getLogger(__name__)


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=8000)
    timezone: str = Field(default="UTC", max_length=64)


WEEKDAYS_TR = ("Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar")
MONTHS_TR = (
    "Ocak",
    "Şubat",
    "Mart",
    "Nisan",
    "Mayıs",
    "Haziran",
    "Temmuz",
    "Ağustos",
    "Eylül",
    "Ekim",
    "Kasım",
    "Aralık",
)


def turn_context(timezone: str, now: datetime | None = None) -> str:
    """Real local date and time for each turn; replaces scripted opening variants."""
    local = (now or datetime.now(ZoneInfo(timezone))).astimezone(ZoneInfo(timezone))
    stamp = (
        f"{local.day} {MONTHS_TR[local.month - 1]} {local.year} {WEEKDAYS_TR[local.weekday()]}, "
        f"saat {local:%H:%M}"
    )
    return f"Kullanıcının saat dilimi: {timezone}. Yerel tarih ve saat: {stamp}.\n\n"


PendingAction = Literal["start", "interrupt"]
Source = Literal["browser", "codex"]

# Seconds without any message before the chat gives up: during a turn / while idle.
ACTIVE_TURN_TIMEOUT_SECONDS = 600
IDLE_TIMEOUT_SECONDS = 1800


async def run_health_tool(
    params: JsonObject, sessions: SessionFactory | None = None
) -> tuple[object, bool]:
    """Answer one ``health_archive`` tool call; returns (result payload, success)."""
    try:
        if params.get("tool") != "health_archive":
            raise ValueError("Bu sağlık aracı desteklenmiyor.")
        arguments = params["arguments"]
        if isinstance(arguments, str):
            arguments = json.loads(arguments)
        result = await read_health(sessions, arguments)
        # A structured refusal (the coverage budget) is not a successful read.
        return result, "error" not in result
    except (ValueError, KeyError):
        return {
            "error": "Sorgu geçersiz. Aracın tarih, saat dilimi ve sayfa sınırlarını kontrol et."
        }, False
    except Exception:
        logger.warning("Umay health archive query failed", exc_info=False)
        return {"error": "Sağlık arşivi okunamadı. Veritabanı bağlantısını kontrol et."}, False


class ChatRelay:
    """Turn state for one browser chat socket bound to one ephemeral Codex thread.

    ``active`` is true from the browser's question until Codex reports ``turn/completed``
    (or rejects ``turn/start``). ``turn_id`` is only known once ``turn/start`` answers, so
    a cancel that arrives earlier is remembered in ``cancel_requested`` and sent as soon as
    the id arrives. ``pending`` maps our outstanding JSON-RPC request ids to their purpose.
    """

    def __init__(
        self,
        websocket: ChatSocket,
        rpc: CodexRPC,
        thread_id: str,
        sessions: SessionFactory | None = None,
    ) -> None:
        self.sessions = sessions
        self.websocket = websocket
        self.rpc = rpc
        self.thread_id = thread_id
        self.active = False
        self.cancel_requested = False
        self.turn_id: str | None = None
        self.pending: dict[int, tuple[PendingAction, HealthTransfer | None]] = {}
        self.transfer: HealthTransfer | None = None
        self.context = ConversationWindow()
        self.tools = HealthQueries()

    async def publish_transfer(self) -> None:
        if self.transfer:
            # Log before browser delivery: a closed browser cannot erase a bridge send.
            receipt = self.transfer.log()
            await self.websocket.send_json({"type": "health_transfer", **receipt})

    async def finish_transfer(self, status: str) -> None:
        if self.transfer and self.transfer.status == "active":
            self.transfer.status = status
            with suppress(Exception):
                await self.publish_transfer()

    @property
    def timeout(self) -> float:
        return ACTIVE_TURN_TIMEOUT_SECONDS if self.active else IDLE_TIMEOUT_SECONDS

    async def send(self, kind: str, text: str) -> None:
        await self.websocket.send_json({"type": kind, "text": text})

    async def interrupt(self, turn_id: str) -> None:
        request_id = await self.rpc.request(
            "turn/interrupt", {"threadId": self.thread_id, "turnId": turn_id}
        )
        self.pending[request_id] = ("interrupt", self.transfer)
        self.cancel_requested = False

    async def interrupt_on_close(self) -> None:
        """Stop a running turn when the browser leaves; the socket may already be gone."""
        if self.active and self.turn_id:
            with suppress(Exception):
                await self.rpc.request(
                    "turn/interrupt", {"threadId": self.thread_id, "turnId": self.turn_id}
                )

    # Browser -> Codex

    async def on_browser(self, message: Any) -> None:
        if not isinstance(message, dict):
            await self.send("error", "Bir soru yaz.")
            return
        if message.get("type") == "cancel":
            await self.cancel()
            return
        if self.active:
            await self.send("notice", "Umay mevcut soruyu yanıtlıyor. Bitmesini bekleyebilirsin.")
            return
        await self.start_turn(cast(JsonObject, message))

    async def cancel(self) -> None:
        self.cancel_requested = self.active
        if self.active:
            self.context.cancelled = True
            self.tools.cancel()
        if self.active and self.turn_id:
            await self.interrupt(self.turn_id)
        if self.active:
            await self.send("working", "Yanıt durduruluyor…")

    async def start_turn(self, message: JsonObject) -> None:
        try:
            body = ChatMessage.model_validate(message)
            if not body.text.strip():
                raise ValueError("Bir soru yaz.")
            ZoneInfo(body.timezone)
        except (ValidationError, ValueError, KeyError):
            await self.send(
                "error",
                "Geçerli bir soru yaz (en fazla 8000 karakter) ve saat dilimini kontrol et.",
            )
            return
        self.transfer = HealthTransfer()
        self.turn_id = None
        self.context.begin(body.text)
        await self.publish_transfer()
        request_id = await self.rpc.request(
            "turn/start",
            {
                "threadId": self.thread_id,
                "input": [
                    {
                        "type": "text",
                        "text": turn_context(body.timezone) + body.text,
                    },
                    {"type": "text", "text": self.context.render()},
                ],
                "model": MODEL,
                "effort": EFFORT,
            },
        )
        self.pending[request_id] = ("start", self.transfer)
        self.active = True
        self.cancel_requested = False
        await self.send("working", "Umay düşünüyor…")

    # Codex -> browser

    async def on_codex(self, message: JsonObject) -> None:
        method = message.get("method")
        params = message.get("params", {})
        if method is None:
            await self.on_response(message)
        elif "id" in message:
            await self.on_server_request(message, method, params)
        elif params.get("threadId") == self.thread_id:
            await self.on_notification(method, params)

    async def on_response(self, message: JsonObject) -> None:
        """Reply to one of our own requests (turn/start or turn/interrupt)."""
        request_id: Any = message.get("id")  # an id we never issued simply matches nothing
        pending = self.pending.pop(request_id, None)
        if pending is None or pending[1] is not self.transfer or not self.active:
            return
        action = pending[0]
        if "error" in message:
            if action == "start":
                self.tools.cancel()
                self.pending.clear()
                self.active = False
                self.context.finish("failed")
                await self.finish_transfer("failed")
            await self.send("error", message["error"].get("message", "Umay isteği tamamlayamadı."))
        elif action == "start":
            turn_id: str = message["result"]["turn"]["id"]
            self.turn_id = turn_id
            if self.cancel_requested:
                await self.interrupt(turn_id)

    async def on_server_request(self, message: JsonObject, method: str, params: JsonObject) -> None:
        """Codex asks us to do something; only this thread's health tool is allowed."""
        if not self.tools.claim(message["id"]):
            return
        if (
            method != "item/tool/call"
            or params.get("threadId") != self.thread_id
            or not self.active
            or self.context.cancelled
            or self.transfer is None
            or (params.get("turnId") and self.turn_id and params["turnId"] != self.turn_id)
            or self.tools.full
        ):
            await self.rpc.reject(message)
            return
        await self.send("working", "Sağlık arşivi sorgulanıyor…")
        self.tools.start(
            ToolCall(message["id"], params, self.transfer), run_health_tool(params, self.sessions)
        )

    async def on_tool_result(self, task: asyncio.Task[ToolResult]) -> bool:
        """Reap exactly once; invalidated queries never send or amend a receipt.

        Returns whether a result reached the bridge, which counts as chat activity.
        """
        call = self.tools.tasks.pop(task)
        current = call.valid and self.active and call.transfer is self.transfer
        try:
            data, success = task.result()
        except asyncio.CancelledError:
            return False
        except Exception:
            if current:
                raise
            return False
        if not current:
            return False
        params = call.params
        if params.get("turnId") and self.turn_id and params["turnId"] != self.turn_id:
            return False
        serialized = json.dumps(data, ensure_ascii=False)
        try:
            receipt = describe_result(params.get("arguments"), json.loads(serialized), success)
        except Exception as exc:
            # The receipt must never cost the model its tool answer. Type name only: an
            # exception message may quote health values.
            logger.warning("Umay health transfer receipt failed: %s", type(exc).__name__)
            receipt = neutral_receipt(success)
        try:
            await self.rpc.send(
                {
                    "id": call.request_id,
                    "result": {
                        "success": success,
                        "contentItems": [{"type": "inputText", "text": serialized}],
                    },
                }
            )
        except Exception as exc:
            call.transfer.failed_sends += 1
            await self.finish_transfer(
                "disconnected" if isinstance(exc, ConnectionClosed) else "failed"
            )
            raise
        call.transfer.calls.append(receipt)
        await self.publish_transfer()
        return True

    async def on_notification(self, method: str, params: JsonObject) -> None:
        if not self.active:
            return
        event_turn = params.get("turnId") or params.get("turn", {}).get("id")
        if event_turn and self.turn_id and event_turn != self.turn_id:
            return
        if method == "item/agentMessage/delta":
            if params["delta"].strip():
                self.context.received_response = True
            await self.websocket.send_json(
                {"type": "delta", "id": params["itemId"], "text": params["delta"]}
            )
        elif method == "item/completed" and params.get("item", {}).get("type") == "agentMessage":
            item = params["item"]
            if item["text"].strip():
                self.context.received_response = True
            await self.websocket.send_json(
                {"type": "message", "id": item["id"], "text": item["text"]}
            )
        elif method == "turn/completed":
            self.tools.cancel()
            self.pending.clear()
            self.context.finish(params["turn"]["status"])
            self.active = False
            self.cancel_requested = False
            self.turn_id = None
            turn = params["turn"]
            await self.finish_transfer(
                turn["status"] if turn["status"] in {"completed", "interrupted"} else "failed"
            )
            if turn["status"] == "failed":
                await self.send(
                    "error", (turn.get("error") or {}).get("message", "Umay yanıt üretemedi.")
                )
            await self.websocket.send_json({"type": "done", "status": turn["status"]})


def _read(websocket: ChatSocket, rpc: CodexRPC, source: Source) -> asyncio.Task[Any]:
    return asyncio.create_task(websocket.receive_json() if source == "browser" else rpc.receive())


async def relay(
    websocket: ChatSocket,
    rpc: CodexRPC,
    thread_id: str,
    *,
    authorized: Callable[[], bool] | None = None,
    revoked: asyncio.Event | None = None,
    sessions: SessionFactory | None = None,
) -> None:
    """Pump both sockets until either side disconnects or the chat times out.

    ``revoked`` is set when the browser's session ends (logout): the socket then closes at once,
    before anything else is forwarded, also while the bridge is still sending.
    """
    chat = ChatRelay(websocket, rpc, thread_id, sessions)
    revocation = asyncio.create_task(revoked.wait()) if revoked is not None else None
    readers: dict[asyncio.Task[Any], Source] = {
        _read(websocket, rpc, "browser"): "browser",
        _read(websocket, rpc, "codex"): "codex",
    }
    deadline = asyncio.get_running_loop().time() + chat.timeout
    try:
        while True:
            waiting: set[asyncio.Task[Any]] = {*readers, *chat.tools.tasks}
            if revocation is not None:
                waiting.add(revocation)
            done, _ = await asyncio.wait(
                waiting,
                timeout=max(0, deadline - asyncio.get_running_loop().time()),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if revocation in done:
                await websocket.close(code=1008)
                return
            # A ready query result must not outlive the socket inactivity deadline.
            if not done or asyncio.get_running_loop().time() >= deadline:
                chat.tools.cancel()
                await chat.finish_transfer("timed_out")
                await chat.send(
                    "error",
                    "Umay yanıtı zaman aşımına uğradı. Sohbeti yeniden bağlayıp deneyebilirsin.",
                )
                return
            # Socket control messages take precedence over query results ready in the
            # same wait. Browser cancellation wins even if both sockets are readable.
            for task in sorted(done & readers.keys(), key=lambda task: readers[task] != "browser"):
                source = readers.pop(task)
                message: Any = task.result()
                readers[_read(websocket, rpc, source)] = source
                # Checked in both directions: bridge messages must not reach a signed-out browser.
                if authorized is not None and not authorized():
                    await websocket.close(code=1008)
                    return
                if source == "browser":
                    await chat.on_browser(message)
                else:
                    await chat.on_codex(message)
                deadline = asyncio.get_running_loop().time() + chat.timeout
            for task in done & chat.tools.tasks.keys():
                if await chat.on_tool_result(task):
                    # A delivered result is activity: the model now works on its answer.
                    deadline = asyncio.get_running_loop().time() + chat.timeout
    except (ChatDisconnected, ConnectionClosed):
        chat.tools.cancel()
        await chat.finish_transfer("disconnected")
        raise
    except Exception:
        chat.tools.cancel()
        await chat.finish_transfer("failed")
        raise
    finally:
        await _close_relay(chat, readers, revocation)


async def _close_relay(
    chat: ChatRelay,
    readers: dict[asyncio.Task[Any], Source],
    revocation: asyncio.Task[bool] | None,
) -> None:
    if revocation is not None:
        revocation.cancel()
    chat.tools.cancel()
    chat.context.clear()
    for task in readers:
        task.cancel()
    await asyncio.gather(*readers, return_exceptions=True)
    await chat.interrupt_on_close()
    await chat.tools.close()
    await chat.finish_transfer("disconnected")
