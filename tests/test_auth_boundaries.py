"""Exact edges of the session and token checks (injected clock: no waiting, no drift)."""

from __future__ import annotations

import pytest
from starlette.datastructures import Headers

from app.core.auth import SessionStore, request_authenticated, same_origin_form


class Clock:
    now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_a_session_ends_exactly_at_its_expiry():
    clock = Clock()
    store = SessionStore(ttl_seconds=10, clock=clock)
    session = store.issue()
    clock.now = 1009.999
    assert store.valid(session)
    clock.now = 1010.0
    assert not store.valid(session)


def test_a_second_session_cookie_does_not_rescue_a_revoked_first():
    store = SessionStore(ttl_seconds=10, clock=Clock())
    alive, revoked = store.issue(), store.issue()
    store.revoke(revoked)
    assert store.valid(alive) and not store.valid(revoked)
    assert not request_authenticated(
        "ui-token", Headers({"cookie": f"other={alive}; healthos_ui={revoked}"}), store
    )


def test_a_token_prefix_is_not_the_token():
    store = SessionStore()
    for candidate in ("ui-toke", "ui-token-x", ""):
        assert not request_authenticated(
            "ui-token", Headers({"authorization": f"Bearer {candidate}"}), store
        )
    assert request_authenticated("ui-token", Headers({"authorization": "bearer ui-token"}), store)


@pytest.mark.parametrize(
    ("origin", "host", "allowed"),
    [
        ("http://localhost:8000", "localhost:8000", True),
        ("http://evil.example:8000", "localhost:8000", False),  # same port, other host
        ("http://user@localhost:8000", "localhost:8000", False),  # hidden credentials in origin
        ("http://localhost:9000", "localhost:8000", False),
    ],
)
def test_login_form_origin_must_be_the_exact_host(origin, host, allowed):
    assert same_origin_form(Headers({"origin": origin, "host": host}), "http") is allowed
