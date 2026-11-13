"""Umay chat test helpers: a fake JSON-RPC peer, a fake browser and held queries."""

from __future__ import annotations

import asyncio


class Browser:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()

    async def receive_json(self):
        item = await self.incoming.get()
        if isinstance(item, Exception):
            raise item
        return item

    async def send_json(self, item):
        await self.outgoing.put(item)


class RPC:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()
        self.sequence = 0

    async def receive(self):
        return await self.incoming.get()

    async def request(self, method, params):
        self.sequence += 1
        await self.outgoing.put({"id": self.sequence, "method": method, "params": params})
        return self.sequence

    async def send(self, item):
        await self.outgoing.put(item)

    async def reject(self, item):
        await self.outgoing.put({"id": item["id"], "error": "unsupported"})


async def take(queue):
    item = await asyncio.wait_for(queue.get(), 30)
    # Receipts are verified separately against real serialized health results.
    return await take(queue) if item.get("type") == "health_transfer" else item


class HeldQuery:
    def __init__(self, *, late=False, error=False):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.cleaned = asyncio.Event()
        self.late = late
        self.error = error
        self.task = None

    async def __call__(self, _sessions, _arguments=None):
        self.task = asyncio.current_task()
        self.entered.set()
        try:
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                if not self.late:
                    raise
                await self.release.wait()
            if self.error:
                raise RuntimeError("synthetic query failure")
            return {"streams": [{"data_type": "heart-rate"}]}
        finally:
            self.cleaned.set()


def tool_request(identifier="tool", turn="turn-1", thread="site-thread"):
    return {
        "id": identifier,
        "method": "item/tool/call",
        "params": {
            "threadId": thread,
            "turnId": turn,
            "tool": "health_archive",
            "arguments": {"action": "inventory"},
        },
    }
