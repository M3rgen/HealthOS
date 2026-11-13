"""tests/support/shuffled_order.py: the same seed gives the same order, on any input order."""

from __future__ import annotations

from dataclasses import dataclass

from support.shuffled_order import shuffled


@dataclass(frozen=True)
class Item:
    nodeid: str


ITEMS = [Item(f"tests/test_x.py::test_{index}") for index in range(40)]


def test_the_same_seed_gives_the_same_order_from_any_starting_order():
    assert shuffled(ITEMS, "7") == shuffled(list(reversed(ITEMS)), "7")


def test_different_seeds_give_different_orders():
    assert shuffled(ITEMS, "7") != shuffled(ITEMS, "8")


def test_no_test_is_lost_or_duplicated():
    assert sorted(shuffled(ITEMS, "9"), key=lambda i: i.nodeid) == sorted(
        ITEMS, key=lambda i: i.nodeid
    )
