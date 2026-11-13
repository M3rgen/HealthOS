"""Tests that need a real local listener, in environments that may forbid one.

Some sandboxes (the headless runner's among them) refuse ``bind`` outright. Such a test can say
nothing there, so it is skipped with the reason, but only when the refusal is the environment's:
checked up front, before the test runs, so a ``PermissionError`` from the code under test still
fails. In CI, or with ``HEALTHOS_REQUIRE_SOCKETS=1``, a refused bind is a failure, never a skip.
"""

from __future__ import annotations

import os
import socket

import pytest

REQUIRED = "HEALTHOS_REQUIRE_SOCKETS"


def require_local_bind(
    *,
    family: socket.AddressFamily = socket.AF_INET,
    address: tuple[str, int] | str = ("127.0.0.1", 0),
) -> None:
    """Skip when the environment refuses a bind, before running the code under test."""
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(address)
        except PermissionError as refused:
            if os.environ.get("CI") or os.environ.get(REQUIRED) == "1":
                pytest.fail(f"a local bind was refused where sockets are required: {refused}")
            pytest.skip(f"this environment refuses a local bind: {refused}")
