"""Umay bridge test helpers: replies of a verified bridge and a scripted JSON-RPC peer."""

from __future__ import annotations

import json

from app.services.umay import codex_bridge_policy as policy


def verified_bridge_config(directory: str = "/srv/bridge") -> dict:
    """``config/read`` result of a bridge started by the current launcher and policy."""
    features: dict = {feature: False for feature in policy.DISABLED_FEATURES}
    features["multi_agent_v2"] = {"enabled": False, "max_concurrent_threads_per_session": 1}
    return {
        "config": {
            "model": policy.MODEL,
            "web_search": "disabled",
            "model_catalog_json": f"{directory}/{policy.catalog_file_name()}",
            "features": features,
        },
        "origins": {},
    }


def attested_thread(thread_id: str) -> dict:
    """thread/start result of a verified runtime (CLI 0.161.0, read-only, no network)."""
    return {
        "thread": {"id": thread_id, "cliVersion": "0.161.0"},
        "model": policy.MODEL,
        "approvalPolicy": "never",
        "sandbox": {"type": "readOnly", "networkAccess": False},
    }


def bridge_replies(**overrides) -> dict:
    """Results by method of a verified bridge; keyword arguments replace single replies."""
    replies = {
        "initialize": {},
        "account/read": {"account": {"type": "chatgpt"}},
        "config/read": verified_bridge_config(),
        "thread/start": attested_thread("t"),
    }
    replies.update({key.replace("_", "/"): value for key, value in overrides.items()})
    return replies


class ScriptedBridge:
    """A websocket connection that answers each request from ``replies`` and records it.

    A method missing from ``replies`` is answered like an app-server without it.
    """

    def __init__(self, replies: dict) -> None:
        self.replies = replies
        self.sent: list[dict] = []
        self.pending: list[str] = []

    async def send(self, text: str) -> None:
        request = json.loads(text)
        self.sent.append(request)
        if "id" in request:
            if request["method"] in self.replies:
                reply = {"id": request["id"], "result": self.replies[request["method"]]}
            else:
                error = {"code": -32601, "message": f"unknown method {request['method']}"}
                reply = {"id": request["id"], "error": error}
            self.pending.append(json.dumps(reply))

    async def recv(self) -> str:
        return self.pending.pop(0)

    def methods(self) -> list[str]:
        return [item["method"] for item in self.sent if "method" in item]


# Runs scripts/start_codex_bridge.py (argv[1]) as a process with the host-dependent checks
# replaced: tests never own the fixed bridge port, which another local process may hold, and
# their synthetic CLIs are Python scripts (native binaries: test_codex_bridge_cli_selection).
LAUNCHER_UNDER_TEST = """\
import runpy, sys
script = sys.argv.pop(1)
launcher = runpy.run_path(script, run_name="bridge_under_test")["main"].__globals__
launcher["listening"] = lambda host: False
launcher["native_executable"] = lambda path: True
launcher["PORT"] = int(__import__("os").environ.get("BRIDGE_TEST_PORT", "0"))
"""
LAUNCHER_MAIN = """\
sys.stdout.reconfigure(encoding="utf-8")
raise SystemExit(launcher["main"]())
"""


# Answers the listen address the launcher gives a fake CLI, like the real app-server: the
# capability token, ``initialize`` and ``config/read`` from its own CODEX_HOME/config.toml.
FAKE_APP_SERVER = """\
def serve_app_server(args, home):
    import json, threading, tomllib
    from http import HTTPStatus
    from pathlib import Path
    from urllib.parse import urlsplit
    from websockets.sync.server import serve
    url = urlsplit(args[args.index("--listen") + 1])
    token = Path(args[args.index("--ws-token-file") + 1]).read_text().strip()
    config = tomllib.loads((home / "config.toml").read_text())
    def reject(connection, request):
        if request.headers.get("Authorization") != f"Bearer {token}":
            return connection.respond(HTTPStatus.UNAUTHORIZED, "")
    def handler(socket):
        for raw in socket:
            message = json.loads(raw)
            if "id" not in message:
                continue
            result = {}
            if message["method"] == "config/read":
                result = {"config": {"model_catalog_json": config.get("model_catalog_json")}}
            socket.send(json.dumps({"id": message["id"], "result": result}))
    server = serve(handler, url.hostname, url.port, process_request=reject)
    threading.Thread(target=server.serve_forever, daemon=True).start()
"""
