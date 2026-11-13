"""What a bridge client may ask of the Codex app-server: the chat's requests and nothing else.

The app-server is a general agent host. Its bearer also buys ``command/exec`` with a sandbox the
caller picks, configuration writes and threads with any tools or sandbox (measured with CLI
0.161.0: a shell as the operating-system user, ``shell_tool=false`` notwithstanding). The
application container holds that bearer, so the limits of ``codex_bridge_policy`` would otherwise
bind only what HealthOS chooses to send.

The TLS front (scripts/codex_bridge_front.py) therefore passes every client frame through
``ChatSession``. A frame is parsed, compared with the exact shape the chat sends and forwarded
re-serialized, so that the app-server cannot read it differently from this code. Anything else
ends the connection. The launcher's own measurements never come here: the runtime probe drives
its isolated CLI over stdio, and the front proves its app-server with the inner token.

Only stdlib imports: the launcher's process loads this module outside the web application.
"""

import json
from collections.abc import Callable
from typing import Any

from .codex_bridge_policy import DISABLED_FEATURES, EFFORT, MODEL, initialize_params

# Tool results are the largest client frames; 200 records or 366 days stay well below this.
MAX_CLIENT_FRAME_BYTES = 8 * 1024 * 1024
# The chat reads at most this much per frame (app/api/codex.py).
MAX_SERVER_FRAME_BYTES = 4 * 1024 * 1024

Params = dict[str, Any]


class Refused(Exception):
    """This frame must not reach the app-server; the connection ends.

    ``reason`` is a fixed text for the operator's log, never data from the frame.
    """

    def __init__(self, reason: str, request_id: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.request_id = request_id


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate key")
    return result


def _no_constant(_name: str) -> None:
    raise ValueError("not JSON")


def parse(frame: str | bytes) -> dict[str, Any]:
    """One JSON object from a text frame; batches and duplicate keys are not the chat's."""
    if not isinstance(frame, str):
        raise Refused("ikili çerçeve")
    try:
        message = json.loads(frame, object_pairs_hook=_unique_keys, parse_constant=_no_constant)
    except (ValueError, RecursionError) as exc:
        raise Refused("bozuk çerçeve") from exc
    if not isinstance(message, dict):
        raise Refused("tek nesne olmayan çerçeve")
    return message


def _same(value: Any, expected: Any) -> bool:
    # Serialized, because Python's == would take 1 for true.
    return json.dumps(value, sort_keys=True) == json.dumps(expected, sort_keys=True)


def _is_text_item(item: Any) -> bool:
    return (
        isinstance(item, dict)
        and set(item) == {"type", "text"}
        and item["type"] == "text"
        and isinstance(item["text"], str)
    )


def _is_tool_result(result: Any) -> bool:
    if not isinstance(result, dict) or set(result) != {"success", "contentItems"}:
        return False
    items = result["contentItems"]
    return (
        isinstance(result["success"], bool)
        and isinstance(items, list)
        and len(items) == 1
        and isinstance(items[0], dict)
        and set(items[0]) == {"type", "text"}
        and items[0]["type"] == "inputText"
        and isinstance(items[0]["text"], str)
    )


def _is_error(error: Any) -> bool:
    return (
        isinstance(error, dict)
        and set(error) == {"code", "message"}
        and type(error["code"]) is int
        and isinstance(error["message"], str)
    )


def _account_answer(result: Any) -> Params:
    """Only whether a ChatGPT login exists: no e-mail address, plan or other account detail."""
    account = result.get("account") if isinstance(result, dict) else None
    kind = account.get("type") if isinstance(account, dict) else None
    return {"account": {"type": kind} if isinstance(kind, str) else None}


def _config_answer(result: Any) -> Params:
    """Only the settings the chat compares with its policy (``bridge_problem``).

    The model catalog's path is cut to its file name, which is all the check reads: the host's
    directories and every other setting stay in the bridge.
    """
    config = result.get("config") if isinstance(result, dict) else None
    config = config if isinstance(config, dict) else {}
    kept: Params = {key: config[key] for key in ("model", "web_search") if key in config}
    catalog = config.get("model_catalog_json")
    if isinstance(catalog, str):
        kept["model_catalog_json"] = catalog.replace("\\", "/").rsplit("/", 1)[-1]
    features = config.get("features")
    if isinstance(features, dict):
        narrowed = {name: features[name] for name in DISABLED_FEATURES if name in features}
        agents = features.get("multi_agent_v2")
        if isinstance(agents, dict):
            narrowed["multi_agent_v2"] = {
                key: agents[key]
                for key in ("enabled", "max_concurrent_threads_per_session")
                if key in agents
            }
        kept["features"] = narrowed
    return {"config": kept}


# What each read may tell the application container: the chat's checks, nothing more.
READ_ANSWERS: dict[str, Callable[[Any], Params]] = {
    "account/read": _account_answer,
    "config/read": _config_answer,
}


def _forwarded(message: dict[str, Any]) -> str:
    text = json.dumps(message, ensure_ascii=False)
    try:
        text.encode()
    except UnicodeEncodeError as exc:  # an escaped lone surrogate has no UTF-8 form
        raise Refused("kodlanamayan metin") from exc
    return text


class ChatSession:
    """The state of one connection: which requests the chat may make at this point."""

    def __init__(self, thread_params: Params) -> None:
        self.thread_params = thread_params
        self.opened = False
        self.initialized = False
        self.thread_request: int | None = None
        self.thread_id: str | None = None
        # Requests the app-server made and the chat has not answered yet, by id.
        self.unanswered: dict[int | str, str] = {}
        # Reads the chat made (once each, before its thread), by id, until they are answered.
        self.reads: dict[int, str] = {}
        self.read_methods: set[str] = set()

    def from_client(self, frame: str | bytes) -> str:
        """The text to forward to the app-server; raises Refused for anything else."""
        message = parse(frame)
        if "method" not in message:
            return self._reply(message)
        method = message["method"]
        request_id = message.get("id")
        usable_id = request_id if type(request_id) is int else None
        check = CHECKS.get(method) if isinstance(method, str) else None
        if check is None:
            raise Refused("izin verilmeyen yöntem", usable_id)
        shape = {"method", "params"} if method == "initialized" else {"id", "method", "params"}
        if set(message) != shape or ("id" in shape and usable_id is None):
            raise Refused("izin verilmeyen istek biçimi", usable_id)
        if not isinstance(message["params"], dict):
            raise Refused("izin verilmeyen parametreler", usable_id)
        params = check(self, message["params"], usable_id)
        return _forwarded({**message, "params": params})

    def from_server(self, frame: str | bytes) -> str | bytes:
        """The frame to pass to the chat; notes what it may answer or address next.

        A read's answer is narrowed to what the chat checks (``READ_ANSWERS``); every other
        frame passes as received. Malformed output ends the connection.
        """
        message = parse(frame)
        request_id = message.get("id")
        if "method" not in message and type(request_id) is int and request_id in self.reads:
            read = self.reads.pop(request_id)
            if "result" in message:
                return _forwarded({**message, "result": READ_ANSWERS[read](message["result"])})
        self._observe(message)
        return frame

    def _observe(self, message: dict[str, Any]) -> None:
        if "method" in message and "id" in message:
            if type(message["id"]) not in (int, str) or not isinstance(message["method"], str):
                raise Refused("bozuk sunucu isteği")
            self.unanswered[message["id"]] = message["method"]
        elif "id" in message and message["id"] == self.thread_request and not self.thread_id:
            thread = (message.get("result") or {}).get("thread") or {}
            if isinstance(thread.get("id"), str):
                self.thread_id = thread["id"]

    def _reply(self, message: dict[str, Any]) -> str:
        request_id = message.get("id")
        if type(request_id) not in (int, str) or request_id not in self.unanswered:
            raise Refused("beklenmeyen yanıt")
        if set(message) == {"id", "error"} and _is_error(message["error"]):
            del self.unanswered[request_id]
            return _forwarded(message)
        if (
            set(message) == {"id", "result"}
            and self.unanswered[request_id] == "item/tool/call"
            and _is_tool_result(message["result"])
        ):
            del self.unanswered[request_id]
            return _forwarded(message)
        raise Refused("izin verilmeyen yanıt", None)

    def _initialize(self, params: Params, request_id: int | None) -> Params:
        if self.opened or not _same(params, initialize_params()):
            raise Refused("izin verilmeyen açılış", request_id)
        self.opened = True
        return params

    def _initialized(self, params: Params, request_id: int | None) -> Params:
        if not self.opened or self.initialized or params:
            raise Refused("izin verilmeyen açılış", request_id)
        self.initialized = True
        return params

    def _read(self, method: str, params: Params, request_id: int | None) -> Params:
        # Once each, between the opening and the thread: the chat's checks, not a query service.
        if (
            not self.initialized
            or params
            or method in self.read_methods
            or self.thread_request is not None
            or request_id is None
        ):
            raise Refused("izin verilmeyen okuma", request_id)
        self.read_methods.add(method)
        self.reads[request_id] = method
        return params

    def _account_read(self, params: Params, request_id: int | None) -> Params:
        return self._read("account/read", params, request_id)

    def _config_read(self, params: Params, request_id: int | None) -> Params:
        return self._read("config/read", params, request_id)

    def _thread_start(self, params: Params, request_id: int | None) -> Params:
        if (
            not self.initialized
            or self.thread_request is not None
            or not _same(params, self.thread_params)
        ):
            raise Refused("izin verilmeyen konuşma ayarları", request_id)
        self.thread_request = request_id
        return self.thread_params

    def _this_thread(self, params: Params) -> bool:
        return self.thread_id is not None and params.get("threadId") == self.thread_id

    def _turn_start(self, params: Params, request_id: int | None) -> Params:
        items = params.get("input")
        if (
            not self._this_thread(params)
            or set(params) != {"threadId", "input", "model", "effort"}
            or params["model"] != MODEL
            or params["effort"] != EFFORT
            or not isinstance(items, list)
            or len(items) != 2
            or not all(_is_text_item(item) for item in items)
        ):
            raise Refused("izin verilmeyen tur", request_id)
        return params

    def _turn_interrupt(self, params: Params, request_id: int | None) -> Params:
        if (
            not self._this_thread(params)
            or set(params) != {"threadId", "turnId"}
            or not isinstance(params["turnId"], str)
        ):
            raise Refused("izin verilmeyen durdurma", request_id)
        return params


Check = Callable[[ChatSession, Params, int | None], Params]
# The whole client vocabulary of the chat (app/services/umay/codex_chat.py, chat_relay.py).
CHECKS: dict[str, Check] = {
    "initialize": ChatSession._initialize,
    "initialized": ChatSession._initialized,
    # Both are the chat's own checks before its thread (codex_chat.start): the ChatGPT login,
    # and the bridge's effective configuration against the policy. Answers are narrowed.
    "account/read": ChatSession._account_read,
    "config/read": ChatSession._config_read,
    "thread/start": ChatSession._thread_start,
    "turn/start": ChatSession._turn_start,
    "turn/interrupt": ChatSession._turn_interrupt,
}
ALLOWED_METHODS = frozenset(CHECKS)
