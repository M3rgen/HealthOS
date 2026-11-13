"""Small JSON-RPC client; health data stays behind the backend's read tools."""

import json
from typing import Any, Protocol

from .codex_bridge_policy import EFFORT as EFFORT  # re-exported for the API
from .codex_bridge_policy import MODEL as MODEL
from .codex_bridge_policy import bridge_problem, initialize_params, thread_problem
from .codex_chat_thread import FOLLOWUP_INSTRUCTIONS as FOLLOWUP_INSTRUCTIONS
from .codex_chat_thread import HEALTH_INSTRUCTIONS as HEALTH_INSTRUCTIONS
from .codex_chat_thread import IDENTITY_INSTRUCTIONS as IDENTITY_INSTRUCTIONS
from .codex_chat_thread import INSTRUCTIONS as INSTRUCTIONS
from .codex_chat_thread import chat_thread_params
from .codex_chat_thread import load_umay_persona as load_umay_persona

JsonObject = dict[str, Any]


class BridgeUnverified(RuntimeError):
    """The bridge or its thread is not the runtime verified under the current policy."""


class Connection(Protocol):
    """The part of a websockets client connection the RPC needs."""

    async def send(self, message: str) -> None: ...

    async def recv(self) -> str | bytes: ...


class CodexRPC:
    def __init__(self, connection: Connection) -> None:
        self.connection = connection
        self.sequence = 0

    async def send(self, payload: JsonObject) -> None:
        await self.connection.send(json.dumps(payload, ensure_ascii=False))

    async def receive(self) -> Any:
        return json.loads(await self.connection.recv())

    async def request(self, method: str, params: JsonObject) -> int:
        self.sequence += 1
        await self.send({"id": self.sequence, "method": method, "params": params})
        return self.sequence

    async def call(self, method: str, params: JsonObject) -> Any:
        request_id = await self.request(method, params)
        while True:
            message = await self.receive()
            if message.get("id") == request_id and "method" not in message:
                if "error" in message:
                    raise RuntimeError(message["error"].get("message", "Codex RPC hatası"))
                return message["result"]
            if "id" in message and "method" in message:
                await self.reject(message)

    async def reject(self, message: JsonObject) -> None:
        await self.send(
            {
                "id": message["id"],
                "error": {
                    "code": -32601,
                    "message": "Bu bağlantı yalnızca sağlık okuma araçlarını destekler.",
                },
            }
        )

    async def start(self) -> tuple[str, str]:
        await self.call("initialize", initialize_params())
        await self.send({"method": "initialized", "params": {}})
        account = await self.call("account/read", {})
        if not account.get("account") or account["account"].get("type") != "chatgpt":
            raise RuntimeError("Yerel Codex'te ChatGPT hesabınla giriş yapmalısın.")
        # Before any thread exists: a bridge started from another (older, hand-written or
        # unverified) configuration never receives the conversation. A bridge without
        # ``config/read`` fails the same way, through the RPC error.
        problem = bridge_problem(await self.call("config/read", {}))
        if problem:
            raise BridgeUnverified(problem)
        result = await self.call("thread/start", chat_thread_params())
        # Before any user text is sent: an unattested runtime or a thread whose effective
        # sandbox differs from the request never receives the conversation.
        if problem := thread_problem(result):
            raise BridgeUnverified(problem)
        return result["thread"]["id"], result.get("model", "Codex")
