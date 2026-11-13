"""Run the collected tests in a seeded random order: ``-p shuffled_order`` (no new dependency).

A test that passes only after some other test ran, or leaves state behind for one, shows up here.
Every xdist worker computes the same order from the same seed, so they stay in agreement.
"""

from __future__ import annotations

import os
import random
from collections.abc import Sequence
from typing import Protocol

import pytest

SEED_VARIABLE = "HEALTHOS_TEST_ORDER_SEED"


class HasNodeId(Protocol):
    nodeid: str


def shuffled[Item: HasNodeId](items: Sequence[Item], seed: str) -> list[Item]:
    """``items`` in the one order this seed gives, whatever order they arrived in."""
    ordered = sorted(items, key=lambda item: item.nodeid)
    random.Random(seed).shuffle(ordered)
    return ordered


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    seed = os.environ.get(SEED_VARIABLE)
    if seed:
        items[:] = shuffled(items, seed)
