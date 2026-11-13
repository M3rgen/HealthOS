"""A refused local bind is a skip only where sockets are not required."""

from __future__ import annotations

import socket

import pytest
from _pytest.outcomes import Failed, Skipped
from support import sockets


class Refusing:
    def __init__(self, error):
        self.error = error

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def bind(self, _address):
        raise self.error


@pytest.fixture
def refused(monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv(sockets.REQUIRED, raising=False)
    monkeypatch.setattr(socket, "socket", lambda *_: Refusing(PermissionError(1, "denied")))
    return monkeypatch


@pytest.mark.parametrize(
    ("family", "address"),
    [(socket.AF_INET, ("127.0.0.1", 0)), (socket.AF_UNIX, "synthetic.sock")],
)
def test_a_refused_bind_skips_with_the_reason(refused, family, address):
    with pytest.raises(Skipped, match="refuses a local bind:.*denied"):
        sockets.require_local_bind(family=family, address=address)


@pytest.mark.parametrize(
    ("family", "address"),
    [(socket.AF_INET, ("127.0.0.1", 0)), (socket.AF_UNIX, "synthetic.sock")],
)
@pytest.mark.parametrize(("name", "value"), [("CI", "true"), (sockets.REQUIRED, "1")])
def test_a_refused_bind_fails_where_sockets_are_required(refused, name, value, family, address):
    refused.setenv(name, value)
    with pytest.raises(Failed, match="sockets are required"):
        sockets.require_local_bind(family=family, address=address)


def test_any_other_bind_error_is_a_failure_of_the_test(monkeypatch):
    monkeypatch.setattr(socket, "socket", lambda *_: Refusing(OSError(98, "in use")))
    with pytest.raises(OSError, match="in use"):
        sockets.require_local_bind()


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({}, (socket.AF_INET, ("127.0.0.1", 0))),
        (
            {"family": socket.AF_UNIX, "address": "synthetic.sock"},
            (socket.AF_UNIX, "synthetic.sock"),
        ),
    ],
)
def test_an_allowed_bind_probes_the_requested_transport_and_closes(monkeypatch, options, expected):
    calls = []

    class Allowed:
        def __enter__(self):
            return self

        def bind(self, address):
            calls.append(address)

        def __exit__(self, *_):
            calls.append("closed")

    def opened(family, kind):
        calls.append((family, kind))
        return Allowed()

    monkeypatch.setattr(socket, "socket", opened)
    sockets.require_local_bind(**options)
    family, address = expected
    assert calls == [(family, socket.SOCK_STREAM), address, "closed"]
