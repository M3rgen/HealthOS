"""The installers print the UI token (and a new receiver token) only to an interactive terminal
(IP-11/4): a pipe, a log file or a CI log gets the .env path and a grep hint instead, and in
GitHub Actions the values are masked before anything else is printed.

install.sh runs in the stub sandbox of test_installer_unix; a pseudo-terminal stands in for the
terminal. install.ps1 runs with -DryRun-free fake checkouts only where pwsh exists.
"""

from __future__ import annotations

import os
import pty
import re
import subprocess
import threading
from pathlib import Path

from test_installer_unix import BASH, HOST_PLATFORM, REAL, Sandbox, parse_env
from test_installer_unix import box as box
from test_installer_unix import pytestmark as pytestmark  # bash on a Unix host


def run_on_terminal(box: Sandbox, *args: str) -> tuple[int, str]:
    """install.sh with stdout on a pseudo-terminal (stdin stays /dev/null: no questions)."""
    master, slave = pty.openpty()
    chunks: list[bytes] = []

    def drain() -> None:
        while True:
            try:
                data = os.read(master, 4096)
            except OSError:
                return
            if not data:
                return
            chunks.append(data)

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    try:
        process = subprocess.run(
            [BASH, str(box.repo / "install.sh"), *args],
            cwd=box.repo,
            env={**box.env, "NO_COLOR": "1"},
            stdin=subprocess.DEVNULL,
            stdout=slave,
            stderr=slave,
            timeout=120,
        )
    finally:
        os.close(slave)
        reader.join(timeout=10)
        os.close(master)
    return process.returncode, b"".join(chunks).decode("utf-8", "replace").replace("\r\n", "\n")


def test_ui_token_is_shown_on_a_terminal(box):
    code, out = run_on_terminal(box, *REAL, "--platform", HOST_PLATFORM, "--mode", "trial")
    assert code == 0, out
    token = parse_env(box.repo / ".env")["APP_UI_TOKEN"]
    assert re.search(rf"UI token: {re.escape(token)}$", out, re.MULTILINE)
    assert "grep APP_UI_TOKEN" not in out


def test_ui_token_is_not_written_to_a_pipe_or_log(box):
    result = box.run(*REAL, "--platform", HOST_PLATFORM, "--mode", "trial")
    assert result.returncode == 0, result.stdout + result.stderr
    env = box.repo / ".env"
    token = parse_env(env)["APP_UI_TOKEN"]
    assert token not in result.stdout + result.stderr
    assert f"grep APP_UI_TOKEN {env}" in result.stdout
    assert "::add-mask::" not in result.stdout  # only inside GitHub Actions


def test_github_actions_masks_the_tokens_first(box):
    box.env["GITHUB_ACTIONS"] = "true"
    result = box.run(
        *REAL, "--platform", HOST_PLATFORM, "--mode", "trial", "--apple-receiver", "yes"
    )
    assert result.returncode == 0, result.stdout + result.stderr
    values = parse_env(box.repo / ".env")
    ui = values["APP_UI_TOKEN"]
    apple = Path(values["APPLE_RECEIVER_TOKEN_HOST_FILE"]).read_text().strip()
    lines = result.stdout.splitlines()
    for secret in (ui, apple):
        mask = f"::add-mask::{secret}"
        assert mask in lines
        # The value appears on no line other than its own mask command.
        assert [line for line in lines if secret in line] == [mask]


def test_new_receiver_token_is_shown_once_on_a_terminal(box):
    code, out = run_on_terminal(
        box, *REAL, "--platform", HOST_PLATFORM, "--mode", "trial", "--apple-receiver", "yes"
    )
    assert code == 0, out
    token = Path(parse_env(box.repo / ".env")["APPLE_RECEIVER_TOKEN_HOST_FILE"]).read_text()
    assert out.count(f"Authorization: Bearer {token.strip()}") == 1
