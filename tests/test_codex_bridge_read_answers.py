"""The two reads the chat needs pass the front once each, and their answers are narrowed.

``account/read`` (is a ChatGPT login there?) and ``config/read`` (does the running bridge match
the policy?) are the chat's own checks before its thread (app/services/codex_chat.py). Dropping
them would remove a fail-closed check; instead the application container learns only what those
checks read: no e-mail address or plan, no host paths, no other setting.
"""

import json

import pytest

from app.services.umay import codex_bridge_policy as policy
from app.services.umay.codex_bridge_filter import ChatSession, Refused
from app.services.umay.codex_chat_thread import chat_thread_params

THREAD = chat_thread_params()
CATALOG = "/home/someone/.config/healthos-bridge/" + policy.catalog_file_name()


def frame(method, params, request_id=1) -> str:
    message = {"method": method, "params": params}
    if request_id is not None:
        message["id"] = request_id
    return json.dumps(message)


def opened() -> ChatSession:
    session = ChatSession(THREAD)
    session.from_client(frame("initialize", policy.initialize_params(), 0))
    session.from_client(frame("initialized", {}, None))
    return session


def full_config() -> dict:
    features = dict.fromkeys(policy.DISABLED_FEATURES, False)
    features["multi_agent_v2"] = {
        "enabled": False,
        "max_concurrent_threads_per_session": 1,
        "extra": "kept in the bridge",
    }
    features["some_new_feature"] = True
    return {
        "config": {
            "model": policy.MODEL,
            "web_search": "disabled",
            "model_catalog_json": CATALOG,
            "features": features,
            "cwd": "/home/someone/private",
            "sandbox_workspace_write": {"writable_roots": ["/home/someone"]},
        },
        "origins": {"model": {"name": {"type": "user", "file": "/home/someone/config.toml"}}},
    }


def answer(session, request_id, result) -> dict:
    text = session.from_server(json.dumps({"id": request_id, "result": result}))
    return json.loads(text)


def test_account_answer_says_only_the_login_type():
    session = opened()
    session.from_client(frame("account/read", {}, 2))
    result = {
        "account": {"type": "chatgpt", "email": "person@example.com", "planType": "plus"},
        "requiresOpenaiAuth": True,
    }
    assert answer(session, 2, result) == {"id": 2, "result": {"account": {"type": "chatgpt"}}}


@pytest.mark.parametrize("result", [{"account": None}, {}, {"account": {"email": "x"}}, []])
def test_a_missing_login_stays_missing(result):
    session = opened()
    session.from_client(frame("account/read", {}, 2))
    assert answer(session, 2, result)["result"] == {"account": None}


def test_config_answer_keeps_what_the_policy_check_reads_and_no_path():
    session = opened()
    session.from_client(frame("config/read", {}, 3))
    narrowed = answer(session, 3, full_config())["result"]
    assert policy.bridge_problem(narrowed) is None  # the chat's check still passes
    text = json.dumps(narrowed)
    assert "/home/someone" not in text and "origins" not in narrowed
    assert set(narrowed["config"]) == {"model", "web_search", "model_catalog_json", "features"}
    assert narrowed["config"]["model_catalog_json"] == policy.catalog_file_name()
    assert "some_new_feature" not in narrowed["config"]["features"]
    assert "extra" not in narrowed["config"]["features"]["multi_agent_v2"]


def test_a_mismatching_bridge_still_fails_the_check_after_narrowing():
    session = opened()
    session.from_client(frame("config/read", {}, 3))
    config = full_config()
    config["config"]["features"]["shell_tool"] = True
    narrowed = answer(session, 3, config)["result"]
    assert policy.bridge_problem(narrowed) == "bridge feature not disabled"


def test_windows_catalog_path_is_cut_to_its_name():
    session = opened()
    session.from_client(frame("config/read", {}, 3))
    config = full_config()
    config["config"]["model_catalog_json"] = "C:\\Users\\someone\\" + policy.catalog_file_name()
    narrowed = answer(session, 3, config)["result"]["config"]
    assert narrowed["model_catalog_json"] == policy.catalog_file_name()


def test_an_error_answer_passes_unchanged():
    session = opened()
    session.from_client(frame("config/read", {}, 3))
    raw = json.dumps({"id": 3, "error": {"code": -32601, "message": "unknown method"}})
    assert session.from_server(raw) == raw


def test_other_server_frames_pass_as_received():
    session = opened()
    session.from_client(frame("account/read", {}, 2))
    # A server request that happens to reuse the read's id is not the read's answer.
    request = json.dumps({"id": 2, "method": "item/tool/call", "params": {"x": 1}})
    assert session.from_server(request) == request
    assert answer(session, 2, {"account": {"type": "chatgpt", "email": "e"}})["result"] == {
        "account": {"type": "chatgpt"}
    }


@pytest.mark.parametrize("method", ["account/read", "config/read"])
def test_each_read_happens_once(method):
    session = opened()
    session.from_client(frame(method, {}, 2))
    with pytest.raises(Refused) as refused:
        session.from_client(frame(method, {}, 3))
    assert refused.value.request_id == 3


@pytest.mark.parametrize("method", ["account/read", "config/read"])
def test_no_read_after_the_thread_starts(method):
    session = opened()
    session.from_client(frame("thread/start", THREAD, 5))
    with pytest.raises(Refused):
        session.from_client(frame(method, {}, 6))


def test_the_chats_own_order_passes():
    session = opened()
    for request_id, method in ((2, "account/read"), (3, "config/read")):
        assert json.loads(session.from_client(frame(method, {}, request_id)))["method"] == method
    assert json.loads(session.from_client(frame("thread/start", THREAD, 4)))["method"] == (
        "thread/start"
    )
