"""The shape of a JSON value and the differences between two shapes (preview fixture contract)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

JSON_KIND = {str: "string", bool: "boolean", int: "number", float: "number", type(None): "null"}


def shape(value: object, *, maps: Iterable[str] = ()) -> dict[str, set[str]]:
    """Every path in a JSON value with the kinds found there.

    ``a[].b`` is field ``b`` of the elements of list ``a``; a path listed in ``maps`` is an object
    keyed by data (metric names and the like), so its children count as one ``.*`` field.
    """
    found: dict[str, set[str]] = {}
    collapsed = set(maps)

    def visit(item: object, path: str) -> None:
        if isinstance(item, dict):
            found.setdefault(path, set()).add("object")
            for key, child in item.items():
                visit(child, f"{path}.{'*' if path in collapsed else key}")
        elif isinstance(item, list):
            found.setdefault(path, set()).add("array")
            for child in item:
                visit(child, f"{path}[]")
        else:
            found.setdefault(path, set()).add(JSON_KIND[type(item)])

    visit(value, "")
    return found


def merged(*shapes: Mapping[str, set[str]]) -> dict[str, set[str]]:
    """The union of several shapes: a field present in one sample is a field of the response."""
    union: dict[str, set[str]] = {}
    for one in shapes:
        for path, kinds in one.items():
            union.setdefault(path, set()).update(kinds)
    return union


def under_empty_containers(
    empty_side: Mapping[str, set[str]], other: Mapping[str, set[str]]
) -> set[str]:
    """Paths of ``other`` inside a list or object that is empty in ``empty_side``.

    An empty list or object is a valid response with no elements to compare, on either side.
    """
    empty = [
        path
        for path, kinds in empty_side.items()
        if kinds & {"array", "object"}
        and not any(item.startswith((f"{path}[]", f"{path}.")) for item in empty_side)
    ]
    return {path for path in other if any(path.startswith((f"{e}[]", f"{e}.")) for e in empty)}


def drift(
    real: Mapping[str, set[str]],
    fixture: Mapping[str, set[str]],
    *,
    real_only: Iterable[str] = (),
    fixture_only: Iterable[str] = (),
) -> list[str]:
    """What separates a fixture from the real response; empty when the fixture is faithful.

    Null is compatible with any kind, in either direction: a sample shows one value of a nullable
    field, and a null in a sample says nothing about the field's type.
    """
    known_real_only = set(real_only) | under_empty_containers(fixture, real)
    known_fixture_only = set(fixture_only) | under_empty_containers(real, fixture)
    found = [
        f"fixture invents {path}" for path in fixture.keys() - real.keys() - known_fixture_only
    ]
    found += [f"fixture lacks {path}" for path in real.keys() - fixture.keys() - known_real_only]
    for path in real.keys() & fixture.keys():
        left, right = real[path] - {"null"}, fixture[path] - {"null"}
        if left and right and not right <= left:
            found.append(f"{path}: real API returns {sorted(left)}, fixture has {sorted(right)}")
    return sorted(found)
