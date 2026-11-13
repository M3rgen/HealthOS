"""Transport contract for the ephemeral chat relay; adapters translate disconnections."""

from typing import Any, Protocol


class ChatDisconnected(Exception):
    """The browser transport closed during a relay operation."""


class ChatSocket(Protocol):
    async def receive_json(self) -> Any: ...

    async def send_json(self, data: Any) -> None: ...

    async def close(self, code: int = 1000) -> None: ...
