"""Health transfer test helpers: the tool call and its receipts."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy

from app.services.umay import chat_relay as codex
from app.services.umay.chat_socket import ChatDisconnected as WebSocketDisconnect

QUESTION = "synthetic-private-question"


SECRET = "synthetic-private-value-and-token"


class Socket:
    def __init__(self):
        self.sent = []
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()
        self.closed = False

    async def send_json(self, message):
        if self.closed:
            raise WebSocketDisconnect()
        self.sent.append(deepcopy(message))
        await self.outgoing.put(message)

    async def receive_json(self):
        message = await self.incoming.get()
        if isinstance(message, Exception):
            raise message
        return message


class Bridge:
    def __init__(self):
        self.sent = []
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()
        self.fail = False
        self.sequence = 0

    async def send(self, message):
        if self.fail:
            raise self.fail if isinstance(self.fail, Exception) else RuntimeError(SECRET)
        self.sent.append(deepcopy(message))
        await self.outgoing.put(message)

    async def request(self, method, params):
        self.sequence += 1
        await self.send({"id": self.sequence, "method": method, "params": params})
        return self.sequence

    async def reject(self, message):
        await self.send({"id": message["id"], "error": "unsupported"})

    async def receive(self):
        message = await self.incoming.get()
        if isinstance(message, Exception):
            raise message
        return message


async def started(sessions=None):
    socket, bridge = Socket(), Bridge()
    chat = codex.ChatRelay(socket, bridge, SECRET, sessions)
    await chat.on_browser({"text": QUESTION})
    return chat, socket, bridge


def receipts(socket):
    return [item for item in socket.sent if item["type"] == "health_transfer"]


async def tool(chat, bridge, arguments):
    await chat.on_server_request(
        {"id": f"private-rpc-id-{len(chat.tools.seen)}"},
        "item/tool/call",
        {"threadId": chat.thread_id, "tool": "health_archive", "arguments": arguments},
    )
    tasks = tuple(chat.tools.tasks)
    await asyncio.gather(*tasks, return_exceptions=True)
    for task in tasks:
        await chat.on_tool_result(task)
    reply = bridge.sent[-1]["result"]
    return json.loads(reply["contentItems"][0]["text"]), reply["success"]


async def complete(chat, status="completed"):
    await chat.on_notification("turn/completed", {"turn": {"status": status}})
