"""Scripted stdio CLI peer. File-backed model transcripts avoid host socket permissions.

This peer never enforces a sandbox: it reports selected synthetic outcomes so the real probe
must decide whether to attest it. Real OS enforcement belongs to the opt-in CLI regression.
"""

import json
import sys
import tomllib
from pathlib import Path


def emit(message):
    print(json.dumps(message), flush=True)


def catalog(mode):
    description = "### `health_archive`\n### `clock__curr_time`"
    if mode == "nested":
        description += "\n### `shell`"
    if mode == "missing-health":
        description = "### `clock__curr_time`"
    tools = [{"type": "custom", "name": "exec", "description": description}]
    if mode == "shell":
        tools.append({"type": "function", "name": "shell"})
    return [{"type": "namespace", "name": "functions", "tools": tools}]


def command_result(mode, code):
    if "print(open(" in code and mode != "read":
        return {"exitCode": 0, "stdout": Path("sentinel.txt").read_text()}
    if "write('CHANGED')" in code:
        if mode == "write-exit":
            return {"exitCode": 0, "stdout": ""}
        if mode == "write-sentinel":
            Path("sentinel.txt").write_text("CHANGED")
    if "socket.create_connection" in code and mode == "network":
        return {"exitCode": 0, "stdout": "CONNECTED"}
    return {"exitCode": 1, "stdout": "denied"}


class Peer:
    def __init__(self, mode, spool):
        self.mode, self.spool, self.sequence, self.pending = mode, spool, 0, None
        self.config = tomllib.loads((Path.home() / "../codex-home/config.toml").read_text())

    def record(self, bodies):
        with (self.spool / "requests.jsonl").open("a") as stream:
            for body in bodies:
                stream.write(json.dumps(body) + "\n")

    def complete(self, thread):
        emit({"method": "turn/completed", "params": {"threadId": thread}})

    def tool_output(self, item, thread):
        call_id, source = item["call_id"], json.dumps(item)
        if "health_archive({" in source:
            self.pending = thread, call_id
            emit(
                {
                    "id": f"callback-{self.sequence}",
                    "method": "item/tool/call",
                    "params": {
                        "threadId": thread,
                        "tool": "health_archive",
                        "arguments": {"action": "inventory"},
                    },
                }
            )
            return
        output = "denied"
        if "ALL_TOOLS" in source:
            output = json.dumps(
                ["health_archive", "shell" if self.mode == "nested-runtime" else "clock__curr_time"]
            )
            if self.mode == "missing-code-mode-host":
                output = "codex-code-mode-host: No such file or directory"
        spawning = "spawn_agent" in source or "followup_task" in source
        if self.mode == "spawn-result" and spawning:
            output = '{"task_name":"/synthetic-child"}'
        body = {
            "input": [{"type": "custom_tool_call_output", "call_id": call_id, "output": output}]
        }
        if self.mode == "image" and "view_image" in source:
            body["input"].append({"type": "input_image", "image_url": "data:image/png"})
        if self.mode == "unanswered":
            body["input"] = []
        self.record([body])
        if spawning and self.mode == "spawn-request":
            self.record([{"input": [{"type": "agent_message"}]}])
        if spawning and self.mode == "spawn-thread":
            emit({"method": "thread/started", "params": {"thread": {"id": "child"}}})

    def handle(self, message):
        method, params = message.get("method"), message.get("params", {})
        if method is None and self.pending:
            thread, call_id = self.pending
            result = message.get("result", {})
            self.record(
                [
                    {
                        "input": [
                            {
                                "type": "custom_tool_call_output",
                                "call_id": call_id,
                                "output": result.get("contentItems", []),
                            }
                        ]
                    }
                ]
            )
            self.pending = None
            self.complete(thread)
            return
        if "id" not in message:
            return
        result = {}
        if method == "config/read":
            result = {"config": self.config}
            if self.mode == "config":
                result["config"]["web_search"] = "enabled"
        elif method == "thread/start":
            self.sequence += 1
            result = {
                "thread": {"id": f"t{self.sequence}", "cliVersion": "0.161.0"},
                "model": "gpt-6.1-sol",
                "approvalPolicy": "never",
                "sandbox": {"type": "readOnly", "networkAccess": False},
            }
            if self.mode == "thread":
                result["approvalPolicy"] = "on-request"
        elif method == "command/exec":
            result = command_result(self.mode, params["command"][-1])
        emit({"id": message["id"], "result": result})
        if method == "turn/start":
            queue = json.loads((self.spool / "queue.json").read_text())
            items = queue[0] if queue else []
            self.record([{"tools": catalog(self.mode), "input": []}])
            if items:
                self.tool_output(items[0], params["threadId"])
            if not self.pending:
                self.complete(params["threadId"])


def main():
    mode, spool, *args = sys.argv[1:]
    if args == ["--version"]:
        print("codex-cli 0.0.1" if mode == "version" else "codex-cli 0.161.0")
    elif args == ["debug", "models", "--bundled"]:
        print(json.dumps({"models": [{"slug": "gpt-6.1-sol", "multi_agent_version": "v2"}]}))
    else:
        peer = Peer(mode, Path(spool))
        for line in sys.stdin:
            peer.handle(json.loads(line))


if __name__ == "__main__":
    main()
