"""TLS front of the Umay bridge, started by start_codex_bridge.py; never run by hand.

The Codex app-server is an external binary that speaks ``ws://`` with a bearer token. On its own,
anything holding its address while it is down would be handed that bearer and the conversation
(app/services/codex_bridge_tls.py), and anything holding the bearer could run commands on this
host. So the application talks to this process instead:

* On the public bridge address it serves TLS with the key derived from the bridge token, which
  the application pins. An impostor cannot complete the handshake, so nothing reaches it.
* The app-server listens on a private loopback port with its own throw-away token. Before any
  caller is forwarded, its bearer is checked here and the listener on that port has to answer
  ``config/read`` with the launcher's random directory. Other local accounts cannot read it, so
  a process that took the port (the app-server died) gets neither the bridge bearer nor a
  message. Its only prize would be the throw-away token, worthless once the app-server is gone.
* The WebSocket ends here: the bridge token never reaches the app-server, and every frame must
  pass the chat's exact allowlist (app/services/codex_bridge_filter.py) or the connection ends.

The bridge token is held by the application container; the inner token is not. What needs more
than the chat (the runtime probe) runs before this process, against its own CLI over stdio.

This process owns the app-server: it exits with it, and stops it when stopped.
"""

import asyncio
import json
import logging
import os
import re
import secrets
import signal
import ssl
import subprocess
import sys
from collections.abc import Sequence
from contextlib import suppress
from http import HTTPStatus
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from websockets.asyncio.client import connect  # noqa: E402
from websockets.asyncio.server import ServerConnection, serve  # noqa: E402
from websockets.exceptions import ConnectionClosed, WebSocketException  # noqa: E402

from app.services.umay.codex_bridge_filter import (  # noqa: E402
    MAX_CLIENT_FRAME_BYTES,
    MAX_SERVER_FRAME_BYTES,
    ChatSession,
    Refused,
)
from app.services.umay.codex_bridge_tls import server_context  # noqa: E402
from app.services.umay.codex_chat_thread import chat_thread_params  # noqa: E402

# A caller on this host sends its opening request at once: anything slower is holding a slot.
UNAUTHENTICATED_SECONDS = 5
HANDSHAKE_TIMEOUT_SECONDS = 5
PROOF_TIMEOUT_SECONDS = 10
# The app-server has to come up within this time; a proof failure in between is expected.
STARTUP_SECONDS = 30
STARTUP_RETRY_SECONDS = 0.1
STOP_GRACE_SECONDS = 4
# One browser chat is one connection: this is a bound against a local flood, not a quota.
MAX_CONNECTIONS = 16
# Connections that have not shown the bearer yet, counted apart so they cannot take the chats'
# places. When full the oldest is dropped, not the newest: nobody can hold the door shut.
MAX_UNAUTHENTICATED = 16


def inner_request(request_id: int, method: str, params: dict[str, Any]) -> str:
    return json.dumps({"id": request_id, "method": method, "params": params})


async def _reply(socket: Any, request_id: int) -> dict[str, Any]:
    while True:
        message = json.loads(await socket.recv())
        if message.get("id") == request_id and "method" not in message:
            return message


async def _genuine_inner(url: str, token: str, marker: str) -> bool:
    async with connect(
        url,
        additional_headers={"Authorization": f"Bearer {token}"},
        open_timeout=PROOF_TIMEOUT_SECONDS,
        close_timeout=1,
        proxy=None,  # HTTPS_PROXY is here for the app-server; this token must not reach it
    ) as socket:
        # The same opening as the chat (app/services/codex_chat.py).
        client = {"name": "healthos_bridge_front", "title": "HealthOS", "version": "0.1.0"}
        await socket.send(
            inner_request(
                1, "initialize", {"clientInfo": client, "capabilities": {"experimentalApi": True}}
            )
        )
        await _reply(socket, 1)
        await socket.send(json.dumps({"method": "initialized", "params": {}}))
        await socket.send(inner_request(2, "config/read", {}))
        config = (await _reply(socket, 2)).get("result", {}).get("config", {})
    catalog = config.get("model_catalog_json")
    return isinstance(catalog, str) and marker in re.split(r"[\\/]", catalog)


async def genuine_inner(url: str, token: str, marker: str) -> bool:
    """Whether the listener on the private port is the app-server this launcher configured."""
    try:
        async with asyncio.timeout(PROOF_TIMEOUT_SECONDS):
            return await _genuine_inner(url, token, marker)
    except (OSError, TimeoutError, ValueError, KeyError, TypeError, AttributeError):
        return False
    except Exception:  # websockets raises its own hierarchy for a peer that is not a server
        return False


def authorized(values: Sequence[str], token: str) -> bool:
    """Whether the Authorization header values are exactly one bearer of the bridge token."""
    if len(values) != 1:
        return False
    scheme, _, credential = values[0].encode(errors="surrogateescape").partition(b" ")
    return scheme.lower() == b"bearer" and secrets.compare_digest(
        credential.strip(), token.encode()
    )


async def _forward(source: Any, target: Any, check: Any) -> None:
    """Pass frames on while ``check`` accepts them; the first refusal propagates."""
    with suppress(ConnectionClosed):
        async for frame in source:
            await target.send(check(frame))


def _counted_connection(front: "Front") -> type[ServerConnection]:
    """Every connection is tracked from the moment TLS is up, also one that never asks."""

    class Counted(ServerConnection):
        def connection_made(self, transport: Any) -> None:
            super().connection_made(transport)
            front.arrived(self)

        def connection_lost(self, exc: Exception | None) -> None:
            front.departed(self)
            super().connection_lost(exc)

    return Counted


class Front:
    def __init__(self, config: dict[str, Any], token: str, inner_token: str) -> None:
        self.inner_port: int = config["inner_port"]
        self.inner_url = f"ws://127.0.0.1:{self.inner_port}"
        self.marker: str = config["marker"]
        self.token = token
        self.inner_token = inner_token
        self.connections = 0
        self.admitted: set[ServerConnection] = set()
        # In arrival order, with the timer that ends each one's time to show the bearer.
        self._waiting: dict[ServerConnection, asyncio.TimerHandle] = {}
        # Taken once: a chat from another version is refused by the policy fingerprint first.
        self.thread_params = chat_thread_params()

    @property
    def waiting(self) -> int:
        """Connections that have not presented the bearer yet."""
        return len(self._waiting)

    def arrived(self, connection: ServerConnection) -> None:
        while len(self._waiting) >= MAX_UNAUTHENTICATED:
            self._drop(next(iter(self._waiting)))
        timer = asyncio.get_running_loop().call_later(
            UNAUTHENTICATED_SECONDS, self._drop, connection
        )
        self._waiting[connection] = timer

    def _drop(self, connection: ServerConnection) -> None:
        timer = self._waiting.pop(connection, None)
        if timer is not None:
            timer.cancel()
            connection.transport.abort()

    def departed(self, connection: ServerConnection) -> None:
        timer = self._waiting.pop(connection, None)
        if timer is not None:
            timer.cancel()
        elif connection in self.admitted:
            self.admitted.discard(connection)
            self.connections -= 1

    def _authenticate(self, connection: ServerConnection, values: Sequence[str]) -> HTTPStatus:
        """A bearer ends the wait; the connection then needs a place among the chats."""
        if not authorized(values, self.token):
            return HTTPStatus.UNAUTHORIZED
        timer = self._waiting.pop(connection, None)
        if timer is not None:
            timer.cancel()
        if self.connections >= MAX_CONNECTIONS:
            return HTTPStatus.SERVICE_UNAVAILABLE
        self.connections += 1
        self.admitted.add(connection)
        return HTTPStatus.OK

    def genuine(self) -> Any:
        return genuine_inner(self.inner_url, self.inner_token, self.marker)

    def listen(self, host: str, port: int, tls: ssl.SSLContext) -> Any:
        """The public listener; awaiting it starts serving."""
        return serve(
            self.relay,
            host,
            port,
            ssl=tls,
            ssl_handshake_timeout=HANDSHAKE_TIMEOUT_SECONDS,
            process_request=self.admit,
            create_connection=_counted_connection(self),
            open_timeout=UNAUTHENTICATED_SECONDS + PROOF_TIMEOUT_SECONDS,
            close_timeout=2,
            ping_interval=None,  # as before: keepalive stays between the chat and its peer
            compression=None,
            max_size=MAX_CLIENT_FRAME_BYTES,
            server_header=None,
        )

    async def admit(self, connection: ServerConnection, request: Any) -> Any:
        """The opening request: the bridge bearer, then the proof of the app-server behind."""
        status = self._authenticate(connection, request.headers.get_all("Authorization"))
        if status is not HTTPStatus.OK:
            return connection.respond(status, "")
        if not await self.genuine():
            print("Umay köprüsü: iç sunucu doğrulanamadı, istek iletilmedi.", file=sys.stderr)
            return connection.respond(HTTPStatus.SERVICE_UNAVAILABLE, "")
        return None

    async def relay(self, outer: ServerConnection) -> None:
        """One chat: every frame of the client is checked, nothing passes unparsed."""
        session = ChatSession(self.thread_params)
        try:
            async with connect(
                self.inner_url,
                additional_headers={"Authorization": f"Bearer {self.inner_token}"},
                open_timeout=PROOF_TIMEOUT_SECONDS,
                close_timeout=1,
                ping_interval=None,
                compression=None,
                max_size=MAX_SERVER_FRAME_BYTES,
                proxy=None,  # see _genuine_inner
            ) as inner:
                await self._pump(outer, inner, session)
        except (OSError, TimeoutError, WebSocketException):
            await outer.close(1011)

    async def _pump(self, outer: ServerConnection, inner: Any, session: ChatSession) -> None:
        def server_frame(frame: str | bytes) -> str | bytes:
            return session.from_server(frame)

        pumps = [
            asyncio.ensure_future(_forward(outer, inner, session.from_client)),
            asyncio.ensure_future(_forward(inner, outer, server_frame)),
        ]
        try:
            await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for pump in pumps:
                pump.cancel()
        results = await asyncio.gather(*pumps, return_exceptions=True)
        refusal = next((item for item in results if isinstance(item, Refused)), None)
        if refusal is not None:
            print(f"Umay köprüsü: çerçeve reddedildi ({refusal.reason}).", file=sys.stderr)
            if refusal.request_id is not None:
                error = {"code": -32601, "message": "Bu köprü bu isteği kabul etmiyor."}
                with suppress(ConnectionClosed):
                    await outer.send(json.dumps({"id": refusal.request_id, "error": error}))
            await outer.close(1008)


async def _wait_until_genuine(front: Front, child: subprocess.Popen[bytes]) -> bool:
    deadline = asyncio.get_running_loop().time() + STARTUP_SECONDS
    while asyncio.get_running_loop().time() < deadline and child.poll() is None:
        if await front.genuine():
            return True
        await asyncio.sleep(STARTUP_RETRY_SECONDS)
    return False


async def run(
    config: dict[str, Any], front: Front, tls: ssl.SSLContext, child: subprocess.Popen[bytes]
) -> int:
    loop = asyncio.get_running_loop()
    stopping = asyncio.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        with suppress(NotImplementedError):  # Windows: the launcher terminates this process
            loop.add_signal_handler(signum, stopping.set)
    exited = asyncio.ensure_future(asyncio.to_thread(child.wait))
    stop = asyncio.ensure_future(stopping.wait())
    server = None
    try:
        ready = asyncio.ensure_future(_wait_until_genuine(front, child))
        await asyncio.wait({ready, stop}, return_when=asyncio.FIRST_COMPLETED)
        if stop.done():
            ready.cancel()
            return 0
        if not ready.result():
            print("Umay köprüsü: iç sunucu başlatılamadı ya da doğrulanamadı.", file=sys.stderr)
            return child.returncode if child.returncode else 1
        server = await front.listen(config["host"].strip("[]"), config["port"], tls)
        await asyncio.wait({exited, stop}, return_when=asyncio.FIRST_COMPLETED)
        if exited.done():
            print("Umay köprüsü: iç sunucu beklenmedik biçimde durdu.", file=sys.stderr)
            return child.returncode if child.returncode else 0
        return 0
    finally:
        if server is not None:
            server.close()
        if child.poll() is None:
            child.terminate()
            try:
                await asyncio.wait_for(asyncio.shield(exited), STOP_GRACE_SECONDS)
            except TimeoutError:
                child.kill()
        await exited
        stop.cancel()


def main() -> int:
    config_path = Path(sys.argv[1])
    os.umask(0o077)
    # A frame can quote health data, so the library's own log is not kept.
    logging.getLogger("websockets").addHandler(logging.NullHandler())
    logging.getLogger("websockets").propagate = False
    config = json.loads(config_path.read_text(encoding="utf-8"))
    home = Path(config["home"])
    token = (home / "bridge.token").read_text(encoding="utf-8").strip()
    inner_token = (home / "inner.token").read_text(encoding="utf-8").strip()
    front = Front(config, token, inner_token)
    tls = server_context(token, home)
    child = subprocess.Popen(config["command"], stdin=subprocess.DEVNULL)
    return asyncio.run(run(config, front, tls, child))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    raise SystemExit(main())
