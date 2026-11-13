"""The chat's vocabulary at the bridge front: exact shapes pass, nothing else does."""

import json

import pytest

from app.services.umay import codex_bridge_filter as bridge_filter
from app.services.umay import codex_bridge_policy as policy
from app.services.umay.codex_bridge_filter import ChatSession, Refused
from app.services.umay.codex_chat_thread import chat_thread_params

THREAD = chat_thread_params()


def frame(method, params, request_id=1) -> str:
    message = {"method": method, "params": params}
    if request_id is not None:
        message["id"] = request_id
    return json.dumps(message, ensure_ascii=False)


def opened() -> ChatSession:
    session = ChatSession(THREAD)
    session.from_client(frame("initialize", policy.initialize_params()))
    session.from_client(frame("initialized", {}, None))
    return session


def threaded(thread_id="thread-1") -> ChatSession:
    session = opened()
    session.from_client(frame("thread/start", THREAD, 5))
    session.from_server(json.dumps({"id": 5, "result": {"thread": {"id": thread_id}}}))
    return session


def turn(**changes) -> dict:
    params = {
        "threadId": "thread-1",
        "input": [{"type": "text", "text": "soru"}, {"type": "text", "text": "özet"}],
        "model": policy.MODEL,
        "effort": policy.EFFORT,
    }
    params.update(changes)
    return params


def test_the_allowlist_is_exactly_what_the_chat_sends():
    assert bridge_filter.ALLOWED_METHODS == {
        "initialize",
        "initialized",
        "account/read",
        "config/read",
        "thread/start",
        "turn/start",
        "turn/interrupt",
    }


@pytest.mark.parametrize(
    "method",
    [
        "command/exec",
        "command/exec/write",
        "command/exec/terminate",
        "config/value/write",
        "config/batchWrite",
        "skills/config/write",
        "thread/shellCommand",
        "thread/resume",
        "thread/fork",
        "fs/readFile",
        "mcpServer/oauth/login",
        "account/login/start",
        "COMMAND/EXEC",
        "",
    ],
)
def test_every_other_method_is_refused_with_its_request_id(method):
    with pytest.raises(Refused) as refused:
        threaded().from_client(frame(method, {}))
    assert refused.value.request_id == 1


@pytest.mark.parametrize(
    "raw",
    [
        "{not json",
        "",
        "[]",
        json.dumps([{"id": 1, "method": "account/read", "params": {}}]),
        "null",
        "1",
        '{"id":1,"method":"account/read","method":"command/exec","params":{}}',
        '{"id":1,"id":2,"method":"account/read","params":{}}',
        '{"id":NaN,"method":"account/read","params":{}}',
        '{"id":1,"method":"account/read","params":{"x":Infinity}}',
        "[" * 100_000,
        '{"id":1,"method":"turn/start","params":{"threadId":"\\ud800"}}',
        b'{"id":1,"method":"account/read","params":{}}',
    ],
)
def test_malformed_frames_and_batches_are_refused(raw):
    with pytest.raises(Refused):
        threaded().from_client(raw)


@pytest.mark.parametrize(
    "raw",
    [
        {"method": "account/read", "params": {}},  # no id
        {"id": "1", "method": "account/read", "params": {}},
        {"id": True, "method": "account/read", "params": {}},
        {"id": 1.0, "method": "account/read", "params": {}},
        {"id": 1, "method": "account/read"},
        {"id": 1, "method": "account/read", "params": []},
        {"id": 1, "method": "account/read", "params": {}, "jsonrpc": "2.0"},
        {"id": 1, "method": ["account/read"], "params": {}},
        {"id": 1, "method": "initialized", "params": {}},  # a notification with an id
    ],
)
def test_request_envelopes_must_be_the_chats(raw):
    with pytest.raises(Refused):
        threaded().from_client(json.dumps(raw))


@pytest.mark.parametrize("method", ["account/read", "config/read"])
def test_reads_take_no_parameters(method):
    session = opened()
    assert json.loads(session.from_client(frame(method, {})))["method"] == method
    with pytest.raises(Refused):
        session.from_client(frame(method, {"includeLayers": True}))


def test_nothing_is_read_before_the_opening():
    with pytest.raises(Refused):
        ChatSession(THREAD).from_client(frame("account/read", {}))
    session = ChatSession(THREAD)
    session.from_client(frame("initialize", policy.initialize_params()))
    with pytest.raises(Refused):  # initialize was answered, initialized is still missing
        session.from_client(frame("account/read", {}))


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"clientInfo": {"name": "other", "version": "1"}},
        {**policy.initialize_params(), "capabilities": {}},
        {**policy.initialize_params(), "extra": True},
    ],
)
def test_the_opening_is_exact(params):
    with pytest.raises(Refused):
        ChatSession(THREAD).from_client(frame("initialize", params))


def test_the_opening_happens_once():
    session = opened()
    with pytest.raises(Refused):
        session.from_client(frame("initialize", policy.initialize_params()))
    with pytest.raises(Refused):
        session.from_client(frame("initialized", {}, None))


def test_initialized_needs_an_opening_and_empty_parameters():
    with pytest.raises(Refused):
        ChatSession(THREAD).from_client(frame("initialized", {}, None))
    session = ChatSession(THREAD)
    session.from_client(frame("initialize", policy.initialize_params()))
    with pytest.raises(Refused):
        session.from_client(frame("initialized", {"x": 1}, None))


def test_the_chats_thread_passes_unchanged():
    forwarded = json.loads(opened().from_client(frame("thread/start", THREAD, 5)))
    assert forwarded == {"id": 5, "method": "thread/start", "params": THREAD}


@pytest.mark.parametrize(
    "changes",
    [
        {"sandbox": "danger-full-access"},
        {"sandbox": "workspace-write"},
        {"approvalPolicy": "on-request"},
        {"ephemeral": False},
        {"ephemeral": 1},  # Python's == takes 1 for true; the app-server does not
        {"model": "another-model"},
        {"allowProviderModelFallback": True},
        {"baseInstructions": "attacker prompt"},
        {"developerInstructions": "attacker persona"},
        {"dynamicTools": []},
        {"serviceName": "other"},
        {"cwd": "/"},
        {"config": {}},
        {"config": {**THREAD["config"], "features.shell_tool": True}},
    ],
)
def test_any_other_thread_is_refused(changes):
    with pytest.raises(Refused) as refused:
        opened().from_client(frame("thread/start", {**THREAD, **changes}, 5))
    assert refused.value.request_id == 5


def test_missing_thread_settings_are_refused():
    partial = {key: value for key, value in THREAD.items() if key != "sandbox"}
    with pytest.raises(Refused):
        opened().from_client(frame("thread/start", partial, 5))


def test_one_thread_per_connection():
    session = threaded()
    with pytest.raises(Refused):
        session.from_client(frame("thread/start", THREAD, 6))


def test_a_thread_is_opened_only_after_the_opening():
    session = ChatSession(THREAD)
    session.from_client(frame("initialize", policy.initialize_params()))
    with pytest.raises(Refused):
        session.from_client(frame("thread/start", THREAD, 5))


def test_the_chats_turn_and_interrupt_pass():
    session = threaded()
    assert json.loads(session.from_client(frame("turn/start", turn(), 7)))["params"] == turn()
    interrupt = {"threadId": "thread-1", "turnId": "turn-1"}
    assert json.loads(session.from_client(frame("turn/interrupt", interrupt, 8)))["params"] == (
        interrupt
    )


@pytest.mark.parametrize(
    "params",
    [
        turn(threadId="another-thread"),
        turn(model="another-model"),
        turn(effort="low"),
        turn(input=[]),
        turn(input=[{"type": "text", "text": "tek"}]),
        turn(input="metin"),
        turn(input=[{"type": "text", "text": "a"}, {"type": "localImage", "path": "/etc/passwd"}]),
        turn(input=[{"type": "text", "text": "a"}, {"type": "text", "text": 1}]),
        turn(input=[{"type": "text", "text": "a"}, {"type": "text", "text": "b", "x": 1}]),
        turn(input=[{"type": "text", "text": "a"}, "b"]),
        turn(sandboxPolicy={"type": "dangerFullAccess"}),
        turn(approvalPolicy="never"),
        turn(cwd="/"),
        turn(outputSchema={}),
        {key: value for key, value in turn().items() if key != "effort"},
    ],
)
def test_any_other_turn_is_refused(params):
    with pytest.raises(Refused):
        threaded().from_client(frame("turn/start", params, 7))


def test_a_turn_needs_this_connections_thread():
    with pytest.raises(Refused):
        opened().from_client(frame("turn/start", turn(), 7))


def test_an_error_reply_to_thread_start_opens_no_thread():
    session = opened()
    session.from_client(frame("thread/start", THREAD, 5))
    session.from_server(json.dumps({"id": 5, "error": {"code": 1, "message": "hayır"}}))
    assert session.thread_id is None
    with pytest.raises(Refused):
        session.from_client(frame("turn/start", turn(threadId="None"), 7))


@pytest.mark.parametrize(
    "params",
    [
        {"threadId": "another", "turnId": "t"},
        {"threadId": "thread-1"},
        {"threadId": "thread-1", "turnId": 1},
        {"threadId": "thread-1", "turnId": "t", "x": 1},
    ],
)
def test_any_other_interrupt_is_refused(params):
    with pytest.raises(Refused):
        threaded().from_client(frame("turn/interrupt", params, 7))


def tool_request(session, request_id="call-1", method="item/tool/call"):
    session.from_server(
        json.dumps({"id": request_id, "method": method, "params": {"threadId": "thread-1"}})
    )


RESULT = {"success": True, "contentItems": [{"type": "inputText", "text": "{}"}]}
REJECTION = {"code": -32601, "message": "Bu bağlantı yalnızca sağlık okuma araçlarını destekler."}


@pytest.mark.parametrize(
    "request_id",
    ["call-1", 42],
)
def test_the_chats_answers_to_a_tool_call_pass(request_id):
    session = threaded()
    tool_request(session, request_id)
    answer = {"id": request_id, "result": RESULT}
    assert json.loads(session.from_client(json.dumps(answer))) == answer


def test_an_answer_is_accepted_once():
    session = threaded()
    tool_request(session)
    session.from_client(json.dumps({"id": "call-1", "result": RESULT}))
    with pytest.raises(Refused):
        session.from_client(json.dumps({"id": "call-1", "result": RESULT}))


def test_any_request_of_the_server_may_be_rejected():
    session = threaded()
    tool_request(session, method="item/commandExecution/requestApproval")
    answer = {"id": "call-1", "error": REJECTION}
    assert json.loads(session.from_client(json.dumps(answer))) == answer


@pytest.mark.parametrize(
    "answer",
    [
        {"id": "unknown", "result": RESULT},
        {"id": "unknown", "error": REJECTION},
        {"id": True, "result": RESULT},
        {"id": ["call-1"], "result": RESULT},
        {"result": RESULT},
    ],
)
def test_answers_need_a_pending_request_of_the_server(answer):
    session = threaded()
    tool_request(session)
    with pytest.raises(Refused):
        session.from_client(json.dumps(answer))


@pytest.mark.parametrize(
    "result",
    [
        {},
        {"decision": "accept"},
        {**RESULT, "x": 1},
        {"success": 1, "contentItems": RESULT["contentItems"]},
        {"success": True, "contentItems": []},
        {"success": True, "contentItems": "x"},
        {"success": True, "contentItems": [{"type": "inputImage", "imageUrl": "x"}]},
        {"success": True, "contentItems": [{"type": "inputText", "text": 1}]},
        {"success": True, "contentItems": [{"type": "inputText", "text": "a", "x": 1}]},
        {"success": True, "contentItems": ["x"]},
        {"success": True, "contentItems": [{"type": "inputText", "text": "a"}] * 2},
    ],
)
def test_tool_results_have_the_chats_shape(result):
    session = threaded()
    tool_request(session)
    with pytest.raises(Refused):
        session.from_client(json.dumps({"id": "call-1", "result": result}))
    assert "call-1" in session.unanswered


def test_only_a_tool_call_can_be_approved():
    session = threaded()
    tool_request(session, method="item/commandExecution/requestApproval")
    with pytest.raises(Refused):
        session.from_client(json.dumps({"id": "call-1", "result": RESULT}))


@pytest.mark.parametrize(
    "error",
    [
        {},
        {"code": "x", "message": "m"},
        {"code": True, "message": "m"},
        {"code": 1, "message": 2},
        {"code": 1, "message": "m", "data": {}},
    ],
)
def test_rejections_have_the_chats_shape(error):
    session = threaded()
    tool_request(session)
    with pytest.raises(Refused):
        session.from_client(json.dumps({"id": "call-1", "error": error}))


def test_a_reply_with_extra_fields_is_refused():
    session = threaded()
    tool_request(session)
    with pytest.raises(Refused):
        session.from_client(json.dumps({"id": "call-1", "result": RESULT, "error": REJECTION}))


@pytest.mark.parametrize(
    "raw",
    [
        "{",
        "[]",
        b"\x00",
        json.dumps({"id": [1], "method": "item/tool/call"}),
        json.dumps({"id": True, "method": "item/tool/call"}),
        json.dumps({"id": 1, "method": 2}),
    ],
)
def test_malformed_server_output_ends_the_connection(raw):
    with pytest.raises(Refused):
        threaded().from_server(raw)


def test_server_notifications_are_not_answers():
    session = threaded()
    session.from_server(json.dumps({"method": "item/completed", "params": {}}))
    assert session.unanswered == {}


def test_forwarded_frames_are_re_serialized():
    session = opened()
    assert session.from_client('{"id" : 9 ,  "method":"account/read","params":{}}') == (
        '{"id": 9, "method": "account/read", "params": {}}'
    )


def test_refusal_carries_no_frame_data():
    with pytest.raises(Refused) as refused:
        threaded().from_client(frame("command/exec", {"command": ["SENTINEL"]}))
    assert "SENTINEL" not in str(refused.value)
