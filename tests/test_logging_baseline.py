"""Every test starts from one root-logger baseline, whatever ran or was imported first."""

import io
import json
import logging

import pytest
from websockets.client import ClientProtocol
from websockets.protocol import OPEN
from websockets.uri import parse_uri

from app.core.logging import RedactSecrets, configure_logging, event

# tests/conftest.py ROOT_TEST_LEVEL; "conftest" is ambiguous as an import name.
ROOT_TEST_LEVEL = logging.INFO


def app_handlers():
    return [
        handler
        for handler in logging.getLogger().handlers
        if any(isinstance(item, RedactSecrets) for item in handler.filters)
    ]


def test_configure_logging_changes_root_inside_a_test():
    configure_logging("ERROR")
    assert logging.getLogger().level == logging.ERROR and len(app_handlers()) == 1
    logging.getLogger("httpx").setLevel(logging.DEBUG)


def test_next_test_starts_from_the_baseline_again():
    assert logging.getLogger().level == ROOT_TEST_LEVEL and app_handlers() == []
    assert logging.getLogger("httpx").level == logging.WARNING


def test_caplog_sees_info_without_set_level(caplog):
    logging.getLogger("app.synthetic").info("synthetic-info-event")
    assert "synthetic-info-event" in caplog.text


def test_refresh_token_is_redacted_in_emitted_json(capsys):
    configure_logging()
    event(logging.getLogger("app.synthetic"), "credential_check", refresh_token="synthetic-only")
    output = capsys.readouterr().out
    record = json.loads(output)
    assert record["event"] == "credential_check"
    assert record["refresh_token"] == "[REDACTED]"
    assert "synthetic-only" not in output


@pytest.mark.parametrize("name", ["websockets", "websockets.client", "websockets.server"])
def test_debug_websocket_headers_and_frames_are_not_emitted(name, capsys):
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    configure_logging("DEBUG")
    # Exercise the library's handshake/frame loggers without opening a socket.
    protocol = ClientProtocol(parse_uri("ws://localhost:8766"), logger=logger)
    request = protocol.connect()
    request.headers["Authorization"] = "Bearer synthetic-bridge-key"
    protocol.send_request(request)
    protocol.state = OPEN
    protocol.send_text(b"synthetic-frame-content")
    logger.warning("synthetic-transport-warning")
    logging.getLogger("app.synthetic").debug("synthetic-app-debug")
    output = capsys.readouterr().out
    assert "synthetic-bridge-key" not in output and "synthetic-frame-content" not in output
    assert "synthetic-transport-warning" in output and "synthetic-app-debug" in output


@pytest.mark.parametrize(
    "credential",
    ["synthetic-credential-123+/=", "prefix,synthetic-credential", 'prefix"synthetic-credential'],
)
@pytest.mark.parametrize(
    "message",
    [
        "> Authorization: Bearer %s",
        "authorization: Basic %s",
        "{'Authorization': 'Bearer %s'}",
        "request Bearer %s rejected",
        "request bEaReR %s rejected",
        "request ApiKey %s rejected",
        "request aPiKeY\t%s rejected",
        "cookie: healthos_ui=%s",
        "Cookie: other=synthetic; healthos_ui=%s; Path=/",
        "refresh_token=%s",
        "client_secret=%s",
        "access_token=%s",
        "{'refresh_token': '%s'}",
        '{"CLIENT_SECRET": "%s"}',
        "Access_Token = %s",
        "{'Cookie': 'healthos_ui=%s'}",
    ],
)
def test_credentials_in_message_text_are_redacted_in_formatted_logs(
    message, credential, capsys, monkeypatch
):
    stream = io.StringIO()
    logger = logging.getLogger("uvicorn.error")
    monkeypatch.setattr(logger, "handlers", [logging.StreamHandler(stream)])
    monkeypatch.setattr(logger, "propagate", True)
    configure_logging("DEBUG")
    logger.debug(message, credential)
    output = capsys.readouterr().out
    assert "synthetic-credential" not in output + stream.getvalue()
    assert "[REDACTED]" in stream.getvalue()
    assert "[REDACTED]" in json.loads(output)["message"]
