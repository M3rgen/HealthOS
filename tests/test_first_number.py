"""Shared field selection retains strict write and permissive read numeric policies."""

import ast
from decimal import Decimal, InvalidOperation
from functools import partial
from pathlib import Path

import pytest

from app.core import normalization
from app.parsers import common


@pytest.mark.parametrize(
    "parser", [normalization.first_number, partial(normalization.first_number, parse=common.number)]
)
@pytest.mark.parametrize(
    "payload, expected",
    [
        ({}, None),
        ({"a": True, "b": "text", "c": "3.25"}, Decimal("3.25")),
        ({"a": 0, "b": 5}, Decimal(0)),
        ({"a": None, "b": "1e3"}, Decimal("1e3")),
    ],
)
def test_first_parsed_candidate_including_zero(parser, payload, expected):
    assert parser(payload, ("a", "b", "c")) == expected


def test_strict_nonfinite_rejection_is_not_replaced_by_permissive_read_policy():
    payload = {"a": "NaN", "b": 42}
    assert partial(normalization.first_number, parse=common.number)(payload, ("a", "b")).is_nan()
    with pytest.raises(InvalidOperation, match="Nonfinite"):
        normalization.first_number(payload, ("a", "b"))


def test_single_selector_and_explicit_read_parsing_policy():
    root = Path(__file__).resolve().parents[1]
    definitions = []
    read_calls = []
    for path in (root / "app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {"first_number"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                names.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name == "first_number"
                )
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
                and node.name == "first_number"
            ):
                definitions.append(path.relative_to(root).as_posix())
            if not isinstance(node, ast.Call):
                continue
            selector = (
                isinstance(node.func, ast.Name)
                and node.func.id in names
                or isinstance(node.func, ast.Attribute)
                and node.func.attr == "first_number"
            )
            # The ingestion normalizer deliberately retains the strict write default.
            if selector and path != root / "app/parsers/normalize.py":
                read_calls.append((path.relative_to(root).as_posix(), node.lineno))
                assert any(keyword.arg == "parse" for keyword in node.keywords), read_calls[-1]
    assert definitions == ["app/core/normalization.py"]
    assert read_calls
