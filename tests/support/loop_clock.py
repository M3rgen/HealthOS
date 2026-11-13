"""Expire asyncio deadlines by jumping the loop's own clock, with no real waiting.

A test that shortens a production budget to milliseconds is at the mercy of the machine: on a
loaded host a healthy read overruns the 20 ms too, and the test reports a timeout that never
happened. Here the budget keeps its real length and the fake stream decides when time has run
out: at its stall point it jumps the running loop's clock past every budget, so ``asyncio.timeout``
and ``loop.time()`` deadlines fire exactly there and nowhere else.
"""

from __future__ import annotations

import asyncio
from weakref import WeakKeyDictionary

# Longer than every response or request budget in the application (RESPONSE_BODY_MAX_SECONDS).
PAST_ALL_BUDGETS = 3600.0

_offsets: WeakKeyDictionary[asyncio.AbstractEventLoop, list[float]] = WeakKeyDictionary()


def advance_loop_clock(seconds: float = PAST_ALL_BUDGETS) -> None:
    """Move the running loop's clock forward; due timers fire on its next iteration."""
    loop = asyncio.get_running_loop()
    offset = _offsets.get(loop)
    if offset is None:
        real, offset = loop.time, [0.0]
        _offsets[loop] = offset
        loop.time = lambda: real() + offset[0]  # type: ignore[method-assign]
    offset[0] += seconds
