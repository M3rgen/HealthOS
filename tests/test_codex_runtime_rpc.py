"""The probe's transport: the synthetic provider and the stdio client of the app-server."""

import asyncio
import http.client
import json
import os
import sys
import threading
from types import SimpleNamespace

import pytest

from app.services.umay.codex_runtime_rpc import (
    HEALTH_MARKER,
    AppServer,
    ProbeError,
    SyntheticResponses,
    catalog,
    decode_png,
    final_message,
    output_text,
    run_command,
    synthetic_png,
)

# A peer speaking the app-server's stdio protocol; the method name selects the behaviour.
PEER = """\
import json, sys
def emit(message):
    print(json.dumps(message), flush=True)
for line in sys.stdin:
    message = json.loads(line)
    method, request_id = message.get("method"), message.get("id")
    if method is None:
        with open("answers.jsonl", "a") as answers:
            answers.write(line)
        continue
    if request_id is None:
        continue
    if method == "die":
        sys.exit(0)
    if method == "fail":
        emit({"id": request_id, "error": {"message": "refused by peer"}})
        continue
    if method == "chatter":
        print("not json", flush=True)
        emit({"method": "thread/started", "params": {"thread": {"id": "foreign"}}})
        emit({"id": "tool", "method": "item/tool/call", "params": {
            "tool": "health_archive", "threadId": "owned"}})
        emit({"id": "other", "method": "item/tool/call", "params": {
            "tool": "health_archive", "threadId": "foreign"}})
        emit({"id": "ask", "method": "item/commandExecution/requestApproval", "params": {}})
    if method == "thread/start":
        die_in_turn = message["params"].get("die_in_turn")
        emit({"method": "thread/started", "params": {"thread": {"id": "owned"}}})
        emit({"id": request_id, "result": {"thread": {"id": "owned"}}})
        continue
    if method == "turn/start":
        emit({"id": request_id, "result": {"turn": {"id": "t"}}})
        if die_in_turn:
            sys.exit(0)
        emit({"method": "turn/completed", "params": {"threadId": "owned"}})
        continue
    emit({"id": request_id, "result": {"echo": method}})
"""


def app_server(tmp_path, thread_params=None):
    script = tmp_path / "peer.py"
    script.write_text(PEER)
    return AppServer(
        [sys.executable, str(script)],
        tmp_path,
        dict(os.environ),
        tmp_path / "peer.log",
        thread_params or {"ephemeral": True},
    )


def post(port, body, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request("POST", "/v1/responses", body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, response.getheader("Content-Type"), response.read().decode()
    finally:
        connection.close()


@pytest.mark.usefixtures("local_sockets")
def test_provider_records_requests_and_answers_with_scripted_items():
    provider = SyntheticResponses()
    try:
        item = {"type": "function_call", "id": "fc", "call_id": "c", "name": "n", "arguments": "{}"}
        provider.queue.append([item])
        status, kind, stream = post(provider.port, json.dumps({"model": "synthetic"}))
        assert (status, kind) == (200, "text/event-stream")
        events = [json.loads(line[6:]) for line in stream.splitlines() if line.startswith("data: ")]
        assert [event["type"] for event in events] == [
            "response.created",
            "response.output_item.added",
            "response.output_item.done",
            "response.completed",
        ]
        assert events[-1]["response"]["output"] == [item]
        assert provider.requests == [{"model": "synthetic"}]
        # An empty queue ends the turn; a body that is not JSON is still recorded.
        _, _, stream = post(provider.port, b"\x1f\x8b", {"Content-Encoding": "gzip"})
        assert final_message()["id"] in stream
        assert provider.requests[-1] == {"undecodable": True, "encoding": "gzip"}
        connection = http.client.HTTPConnection("127.0.0.1", provider.port, timeout=10)
        connection.request("GET", "/v1/models")
        assert json.loads(connection.getresponse().read()) == {"models": [], "data": []}
        connection.close()
    finally:
        provider.close()


def test_png_decoder_is_a_real_control():
    image = synthetic_png()
    assert len(decode_png(image)) == 14
    with pytest.raises(ValueError, match="not a PNG"):
        decode_png(b"GIF89a")
    with pytest.raises(ValueError, match="CRC"):
        decode_png(image[:20] + bytes([image[20] ^ 1]) + image[21:])


def test_catalog_walks_namespaces_and_additional_tools():
    body = {
        "tools": [
            "not a tool",
            {
                "type": "namespace",
                "name": "functions",
                "tools": [{"name": "exec", "description": "### `a`\n### `b`"}],
            },
        ],
        "input": [
            "skipped",
            {"type": "additional_tools", "tools": [{"name": "extra"}]},
        ],
    }
    assert catalog(body) == ({"functions.exec", "extra"}, {"a", "b"})


def test_output_text_joins_parts():
    assert output_text("plain") == "plain"
    assert output_text([{"text": "a"}, "skipped", {"text": "b"}]) == "a\nb"
    assert output_text(None) == ""


async def test_app_server_reports_errors_and_answers_only_owned_tool_calls(tmp_path):
    async with app_server(tmp_path) as server:
        with pytest.raises(ProbeError, match="fail failed: refused by peer"):
            await server.call("fail", {})
        await server.thread(tmp_path)
        assert server.threads == {"owned"} and server.foreign_threads == []
        await server.call("chatter", {})
        answers = await written_answers(tmp_path, 3)
        assert server.foreign_threads == ["foreign"]
        methods = [request["method"] for request in server.server_requests]
        assert methods == [
            "item/tool/call",
            "item/tool/call",
            "item/commandExecution/requestApproval",
        ]
    # Only the health tool of a thread of this probe gets a result.
    assert HEALTH_MARKER in json.dumps(answers["tool"]["result"])
    assert "result" not in answers["other"] and "error" in answers["other"]
    assert "result" not in answers["ask"] and "error" in answers["ask"]


async def written_answers(root, count):
    path = root / "answers.jsonl"
    for _ in range(200):
        lines = path.read_text().splitlines() if path.exists() else []
        if len(lines) >= count:
            return {answer["id"]: answer for answer in map(json.loads, lines)}
        await asyncio.sleep(0.02)  # the peer writes them after the reply this test waited for
    raise AssertionError(f"the peer received {len(lines)} answers")


async def test_app_server_exit_fails_the_waiter_and_later_calls(tmp_path):
    async with app_server(tmp_path) as server:
        with pytest.raises(ProbeError, match="exited"):
            await server.call("die", {})
        with pytest.raises(ProbeError, match="exited"):
            await server.call("anything", {})


async def test_turn_reports_an_exit_during_the_turn(tmp_path):
    provider = SimpleNamespace(requests=[], queue=[], lock=threading.Lock())
    async with app_server(tmp_path, {"die_in_turn": True}) as server:
        with pytest.raises(ProbeError, match="exited during a probe turn"):
            await server.turn(provider, tmp_path, [], 0)


async def test_a_failing_cli_command_is_reported(tmp_path):
    fail = [sys.executable, "-c", "raise SystemExit(3)"]
    with pytest.raises(ProbeError, match="exited with 3"):
        await run_command(fail, dict(os.environ), tmp_path)
    ok = [sys.executable, "-c", "print('hello')"]
    assert (await run_command(ok, dict(os.environ), tmp_path)).strip() == "hello"
