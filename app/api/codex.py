"""Same-origin site chat proxy to the authenticated local Codex app-server."""

import asyncio
import logging
import ssl
from contextlib import suppress
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from app.api.chat_socket import BrowserSocket
from app.core.auth import request_authenticated, session_cookies
from app.db.contracts import SessionFactory
from app.db.session import SessionLocal
from app.services.umay.chat_relay import relay
from app.services.umay.chat_socket import ChatDisconnected
from app.services.umay.codex_bridge_tls import client_context, secure_bridge_url
from app.services.umay.codex_chat import EFFORT, BridgeUnverified, CodexRPC

router = APIRouter(prefix="/api/codex", tags=["codex"])
logger = logging.getLogger(__name__)

# The browser reconnects with backoff while the chat panel is open, so an absent bridge
# (e.g. a server deployment without the host Codex CLI) would log on every attempt.
# Warn once per outage; repeats go to debug until a connection succeeds again.
_bridge_unavailable_logged = False


def _log_bridge_unavailable(reason: str | None = None) -> None:
    global _bridge_unavailable_logged
    if _bridge_unavailable_logged:
        logger.debug("Local Codex chat connection still unavailable")
        return
    _bridge_unavailable_logged = True
    # ``reason`` is a fixed policy message (BridgeUnverified), never data from the bridge.
    logger.warning(
        "Local Codex chat connection unavailable%s; further failures are logged at debug "
        "level until the bridge connects",
        f" (refused: {reason}; restart the bridge with scripts/start_codex_bridge.py)"
        if reason
        else "",
        exc_info=False,
    )


def _log_bridge_available() -> None:
    global _bridge_unavailable_logged
    if _bridge_unavailable_logged:
        logger.info("Local Codex chat connection restored")
    _bridge_unavailable_logged = False


def same_origin(origin: str | None, host: str | None) -> bool:
    if not origin or not host:
        return False
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and parsed.netloc == host


CONNECT_TIMEOUT_SECONDS = 10
START_TIMEOUT_SECONDS = 60


def chat_sessions() -> SessionFactory:
    """Default archive factory; injectable without opening a session for the socket's life."""
    return SessionLocal


@router.websocket("/chat")
async def chat(
    websocket: WebSocket, archive_sessions: Annotated[SessionFactory, Depends(chat_sessions)]
) -> None:
    def authorized() -> bool:
        return request_authenticated(
            websocket.app.state.settings.app_ui_token,
            websocket.headers,
            getattr(websocket.app.state, "ui_sessions", None),
        )

    if (
        not same_origin(websocket.headers.get("origin"), websocket.headers.get("host"))
        or not authorized()
    ):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    settings = websocket.app.state.settings
    if not (settings.codex_bridge_token or "").strip():
        await websocket.send_json(
            {
                "type": "error",
                "text": "Umay bağlantısı henüz yapılandırılmamış. Bağlantı ayarlarını kontrol et.",
            }
        )
        await websocket.close()
        return
    phase = "connect"
    revoked = asyncio.Event()
    loop = asyncio.get_running_loop()
    sessions = getattr(websocket.app.state, "ui_sessions", None)
    # Logging out ends the chat at once; a bearer-token caller has no session to end.
    unwatch = [
        sessions.watch(session_id, lambda: loop.call_soon_threadsafe(revoked.set))
        for session_id in session_cookies(websocket.headers)
        if sessions is not None and sessions.valid(session_id)
    ]
    try:
        # The TLS handshake proves the peer holds the key derived from the token; no bearer and
        # no message leave before it (app/services/umay/codex_bridge_tls.py).
        async with connect(
            secure_bridge_url(settings.codex_bridge_url),
            ssl=client_context(settings.codex_bridge_token),
            additional_headers={"Authorization": f"Bearer {settings.codex_bridge_token}"},
            open_timeout=CONNECT_TIMEOUT_SECONDS,
            close_timeout=2,
            max_size=4 * 1024 * 1024,
            proxy=None,  # the bridge is on this host: no proxy may stand between (see front)
        ) as connection:
            rpc = CodexRPC(connection)
            phase = "start"
            async with asyncio.timeout(START_TIMEOUT_SECONDS):
                thread_id, model = await rpc.start()
            phase = "ready"
            _log_bridge_available()
            await websocket.send_json({"type": "ready", "model": model, "effort": EFFORT})
            await relay(
                BrowserSocket(websocket),
                rpc,
                thread_id,
                authorized=authorized,
                revoked=revoked,
                sessions=archive_sessions,
            )
    except (WebSocketDisconnect, ChatDisconnected):
        pass
    except Exception as exc:
        text = _bridge_error_text(exc, phase)
        with suppress(Exception):
            await websocket.send_json({"type": "error", "text": text})
    finally:
        for stop in unwatch:
            stop()
        with suppress(Exception):
            await websocket.close()


def _bridge_error_text(exc: Exception, phase: str) -> str:
    reason = str(exc) if isinstance(exc, BridgeUnverified) else None
    # A listener that accepts the connection but cannot complete TLS: a bridge from before
    # the pinned key, or an impostor. Nothing has been sent to it.
    unproven = isinstance(exc, ssl.SSLError) or (
        phase == "connect" and isinstance(exc, ConnectionResetError)
    )
    if unproven:
        reason = "bridge identity not proven (an old or impostor listener)"
    _log_bridge_unavailable(reason)
    if isinstance(exc, BridgeUnverified) or unproven:
        text = (
            "Umay bağlantısı güvenlik doğrulamasından geçmedi. Umay köprüsünü yeniden "
            "başlatman gerekiyor."
        )
    elif isinstance(exc, TimeoutError):
        text = (
            "Umay’ın hazırlanması uzun sürdü. Bağlantı yeniden denenecek."
            if phase == "start"
            else "Umay’a bağlanırken zaman aşımı oldu. Bağlantı yeniden denenecek."
        )
    elif isinstance(exc, ConnectionClosed) or phase == "ready":
        text = "Umay ile bağlantı kesildi. Bağlantı yeniden denenecek."
    elif isinstance(exc, OSError):
        text = "Umay’a şu an ulaşılamıyor. Bağlantı yeniden denenecek."
    else:
        text = "Umay bağlantısı kurulamadı. Bağlantı ayarlarını ve oturumu kontrol et."
    return text
