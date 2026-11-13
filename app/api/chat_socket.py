"""Translate the ASGI WebSocket transport into the chat service contract."""

from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from app.services.umay.chat_socket import ChatDisconnected


class BrowserSocket:
    def __init__(self, websocket: WebSocket) -> None:
        self.websocket = websocket

    async def receive_json(self) -> Any:
        try:
            return await self.websocket.receive_json()
        except WebSocketDisconnect as exc:
            raise ChatDisconnected() from exc

    async def send_json(self, data: Any) -> None:
        try:
            await self.websocket.send_json(data)
        except WebSocketDisconnect as exc:
            raise ChatDisconnected() from exc

    async def close(self, code: int = 1000) -> None:
        try:
            await self.websocket.close(code=code)
        except WebSocketDisconnect as exc:
            raise ChatDisconnected() from exc
