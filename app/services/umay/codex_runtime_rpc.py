"""Real Codex CLI app-server driven over stdio against a synthetic loopback provider.

Shared by the bridge runtime probe (``codex_runtime_probe``) and its regression tests. The
provider records every model request and answers with scripted output items, so a test sees
exactly what the CLI offered and sent to the model. Only stdlib imports.
"""

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import re
import struct
import threading
import zlib
from collections.abc import Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

PROBE_TOOL = {
    "type": "function",
    "name": "health_archive",
    "description": "Synthetic health archive used only by the bridge runtime probe.",
    "inputSchema": {
        "type": "object",
        "properties": {"action": {"type": "string"}},
        "required": ["action"],
        "additionalProperties": False,
    },
}
HEALTH_MARKER = "synthetic-health-probe-result"
CHILD_MARKER = "SYNTHETIC-CHILD-TASK"
SPAWNED = re.compile(r'"task_name"\s*:\s*"/')
TURN_TIMEOUT_SECONDS = 60
SETTLE_SECONDS = 0.6


def synthetic_png() -> bytes:
    """A valid 2x2 RGB PNG (red row, green row)."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    rows = b"\0" + b"\xff\0\0" * 2 + b"\0" + b"\0\xff\0" * 2
    header = struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def decode_png(data: bytes) -> bytes:
    """Independent minimal decoder (CRC + inflate) used as the PNG's positive control."""
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("not a PNG")
    position, pixels = 8, b""
    while position < len(data):
        (length,) = struct.unpack(">I", data[position : position + 4])
        kind = data[position + 4 : position + 8]
        body = data[position + 8 : position + 8 + length]
        (crc,) = struct.unpack(">I", data[position + 8 + length : position + 12 + length])
        if zlib.crc32(kind + body) & 0xFFFFFFFF != crc:
            raise ValueError("PNG CRC mismatch")
        if kind == b"IDAT":
            pixels += body
        position += 12 + length
    return zlib.decompress(pixels)


class SyntheticResponses:
    """Loopback Responses API: scripted output items, every request recorded."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.queue: list[list[dict[str, Any]]] = []
        self.lock = threading.Lock()
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass

            def do_POST(self) -> None:  # noqa: N802 (http.server API)
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                try:
                    body = json.loads(raw)
                except ValueError:
                    body = {"undecodable": True, "encoding": self.headers.get("Content-Encoding")}
                with provider.lock:
                    provider.requests.append(body)
                    items = provider.queue.pop(0) if provider.queue else [final_message()]
                events = [{"type": "response.created", "response": {"id": "r", "status": "x"}}]
                for index, item in enumerate(items):
                    events.append(
                        {"type": "response.output_item.added", "output_index": index, "item": item}
                    )
                    events.append(
                        {"type": "response.output_item.done", "output_index": index, "item": item}
                    )
                usage = {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
                events.append(
                    {
                        "type": "response.completed",
                        "response": {
                            "id": "r",
                            "status": "completed",
                            "output": items,
                            "usage": usage,
                        },
                    }
                )
                payload = "".join(
                    f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self) -> None:  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"models":[],"data":[]}')

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def final_message() -> dict[str, Any]:
    return {
        "type": "message",
        "id": "msg_probe_final",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "Synthetic probe turn finished."}],
    }


def function_call(call_id: str, name: str, arguments: dict, namespace: str | None = None) -> dict:
    item = {
        "type": "function_call",
        "id": f"fc_{call_id}",
        "call_id": call_id,
        "name": name,
        "arguments": json.dumps(arguments),
    }
    if namespace:
        item["namespace"] = namespace
    return item


def exec_call(call_id: str, source: str) -> dict:
    return {
        "type": "custom_tool_call",
        "id": f"ct_{call_id}",
        "call_id": call_id,
        "name": "exec",
        "namespace": "functions",
        "input": source,
    }


def catalog(body: dict[str, Any]) -> tuple[set[str], set[str]]:
    """Top-level tool names (``namespace.name``) and the tools nested inside ``exec``."""
    top: set[str] = set()
    nested: set[str] = set()

    def walk(tools: Any, prefix: str = "") -> None:
        for tool in tools or []:
            if not isinstance(tool, dict):
                continue
            name = str(tool.get("name"))
            if tool.get("type") == "namespace":
                walk(tool.get("tools"), f"{name}.")
                continue
            top.add(prefix + name)
            if name == "exec":
                nested.update(re.findall(r"^### `([^`]+)`", tool.get("description", ""), re.M))

    walk(body.get("tools"))
    for item in body.get("input") or []:
        if isinstance(item, dict) and item.get("type") == "additional_tools":
            walk(item.get("tools"))
    return top, nested


def call_outputs(requests: Sequence[dict[str, Any]], call_id: str) -> list[Any]:
    return [
        item.get("output")
        for body in requests
        for item in body.get("input") or []
        if isinstance(item, dict)
        and item.get("call_id") == call_id
        and str(item.get("type", "")).endswith("_call_output")
    ]


def output_text(output: Any) -> str:
    if isinstance(output, str):
        return output
    return "\n".join(str(part.get("text", "")) for part in output or [] if isinstance(part, dict))


def carries_image(blob: str, image: bytes) -> bool:
    encoded = base64.b64encode(image).decode()
    return any(
        marker in blob
        for marker in ('"input_image"', "data:image", encoded, hashlib.sha256(image).hexdigest())
    )


class AppServer:
    """Minimal JSON-RPC client of ``codex app-server --listen stdio://``."""

    def __init__(
        self,
        command: Sequence[str],
        cwd: Path,
        env: dict[str, str],
        log: Path,
        thread_params: dict[str, Any],
    ) -> None:
        self.command, self.cwd, self.env, self.log = list(command), cwd, env, log
        self.thread_params = thread_params
        self.sequence = 0
        self.pending: dict[int, asyncio.Future] = {}
        self.events: list[dict[str, Any]] = []
        self.completed: dict[str, asyncio.Event] = {}
        self.exited = False
        self.threads: set[str] = set()
        self.foreign_threads: list[str] = []
        self.server_requests: list[dict[str, Any]] = []
        self.process: asyncio.subprocess.Process | None = None
        self.reader: asyncio.Task | None = None

    async def __aenter__(self) -> "AppServer":
        with self.log.open("wb") as stderr:
            self.process = await asyncio.create_subprocess_exec(
                *self.command,
                "app-server",
                "--listen",
                "stdio://",
                cwd=self.cwd,
                env=self.env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=stderr,
                limit=64 * 1024 * 1024,
                start_new_session=os.name != "nt",
            )
        self.reader = asyncio.create_task(self.read())
        await self.call(
            "initialize",
            {
                "clientInfo": {"name": "healthos_bridge_probe", "version": "1.0.0"},
                "capabilities": {"experimentalApi": True},
            },
        )
        await self.send({"method": "initialized", "params": {}})
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        process = self.process
        if process and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
        if self.reader:
            self.reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.reader

    async def read(self) -> None:
        if self.process is None or self.process.stdout is None:
            raise ProbeError("app-server output is unavailable")
        while line := await self.process.stdout.readline():
            try:
                message = json.loads(line)
            except ValueError:
                continue
            self.events.append(message)
            if "id" in message and "method" not in message:
                future = self.pending.pop(message["id"], None)
                if future and not future.done():
                    future.set_result(message)
                continue
            method = message.get("method")
            params = message.get("params") or {}
            if "id" in message:
                await self.on_request(message, method, params)
            elif method == "thread/started":
                thread_id = (params.get("thread") or {}).get("id")
                if thread_id and thread_id not in self.threads:
                    self.foreign_threads.append(thread_id)
            elif method == "turn/completed":
                event = self.completed.get(params.get("threadId", ""))
                if event:
                    event.set()
        # The app-server exited: fail every waiter now instead of after its timeout.
        self.exited = True
        for future in self.pending.values():
            if not future.done():
                future.set_exception(ProbeError("app-server exited"))
        for event in self.completed.values():
            event.set()

    async def on_request(self, message: dict, method: str, params: dict) -> None:
        self.server_requests.append(
            {"method": method, "threadId": params.get("threadId"), "tool": params.get("tool")}
        )
        if (
            method == "item/tool/call"
            and params.get("tool") == "health_archive"
            and params.get("threadId") in self.threads
        ):
            result = {
                "success": True,
                "contentItems": [{"type": "inputText", "text": json.dumps({HEALTH_MARKER: True})}],
            }
            await self.send({"id": message["id"], "result": result})
        else:
            error = {"code": -32601, "message": "Rejected by the bridge runtime probe."}
            await self.send({"id": message["id"], "error": error})

    async def send(self, message: dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            raise ProbeError("app-server input is unavailable")
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        await self.process.stdin.drain()

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self.exited:
            raise ProbeError("app-server exited")
        self.sequence += 1
        future = asyncio.get_running_loop().create_future()
        self.pending[self.sequence] = future
        await self.send({"id": self.sequence, "method": method, "params": params})
        reply = await asyncio.wait_for(future, TURN_TIMEOUT_SECONDS)
        if "error" in reply:
            raise ProbeError(f"{method} failed: {reply['error'].get('message', 'error')}")
        return reply["result"]

    async def thread(self, cwd: Path) -> dict[str, Any]:
        params = dict(self.thread_params, cwd=str(cwd))
        # Recorded before the call: the started notification may precede the reply.
        result = await self.call("thread/start", params)
        self.threads.add(result["thread"]["id"])
        if result["thread"]["id"] in self.foreign_threads:
            self.foreign_threads.remove(result["thread"]["id"])
        return result

    async def turn(
        self, provider: SyntheticResponses, cwd: Path, items: list[dict], settle: float
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        started = await self.thread(cwd)
        thread_id = started["thread"]["id"]
        before = len(provider.requests)
        with provider.lock:
            provider.queue[:] = [items, [final_message()]] if items else []
        self.completed[thread_id] = asyncio.Event()
        await self.call(
            "turn/start",
            {"threadId": thread_id, "input": [{"type": "text", "text": "Synthetic probe turn."}]},
        )
        await asyncio.wait_for(self.completed[thread_id].wait(), TURN_TIMEOUT_SECONDS)
        if self.exited:
            raise ProbeError("app-server exited during a probe turn")
        await asyncio.sleep(settle)
        with provider.lock:
            provider.queue.clear()
            return started, list(provider.requests[before:])


class ProbeError(RuntimeError):
    def __init__(self, message: str, report: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.report = report


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


# The only variables of the operator's environment a Codex CLI process of the bridge receives,
# in the probe and in service alike: nothing else (NODE_OPTIONS, LD_PRELOAD, DYLD_*, RUST_LOG,
# OPENAI_API_KEY, terminal integrations) may change or log what was measured.
PASSED_ENV = ("PATH",)
WINDOWS_ENV = ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "APPDATA", "LOCALAPPDATA")
# Transport to the real model provider only (proxies, CA bundles); added for the service, not
# for the probe, whose provider is on loopback. They do not change the tool surface.
NETWORK_ENV = (
    "HTTPS_PROXY",
    "https_proxy",
    "HTTP_PROXY",
    "http_proxy",
    "ALL_PROXY",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
)


def runtime_env(
    home: Path, codex_home: Path, tmpdir: Path, *, network: bool = False
) -> dict[str, str]:
    """Environment of a bridge CLI process, built from the allowlist above."""
    env = {
        "HOME": str(home),
        "CODEX_HOME": str(codex_home),
        "TMPDIR": str(tmpdir),
        "LANG": "C.UTF-8",
        "PATH": os.environ.get("PATH", os.defpath),
    }
    passed = (*PASSED_ENV, *(WINDOWS_ENV if os.name == "nt" else ()))
    passed += NETWORK_ENV if network else ()
    env.update({key: os.environ[key] for key in passed if key in os.environ and key not in env})
    if os.name == "nt":
        env["USERPROFILE"] = str(home)
    return env


def isolated_env(home: Path, codex_home: Path, scratch: Path) -> dict[str, str]:
    """The probe's environment: the service's allowlist without the provider transport."""
    return runtime_env(home, codex_home, scratch)


async def run_command(command: Sequence[str], env: dict[str, str], cwd: Path) -> str:
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), 60)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise ProbeError(f"{' '.join(command[-2:])} timed out") from None
    if process.returncode != 0:
        raise ProbeError(f"{' '.join(command[-2:])} exited with {process.returncode}")
    return stdout.decode("utf-8", "replace")
