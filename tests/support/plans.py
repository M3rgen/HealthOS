"""Only the repository's explicitly fictional plan is used in tests."""

import json
from copy import deepcopy
from pathlib import Path

from app.services.operations.plan_validation import load_document

SAMPLE = Path(__file__).resolve().parents[2] / "docs/examples/plan.sample.json"


def sample() -> dict:
    return load_document(SAMPLE.read_bytes())


def changed(document: dict, path: tuple, value) -> dict:
    result = deepcopy(document)
    target = result
    for part in path[:-1]:
        target = target[part]
    if path:
        target[path[-1]] = value
        return result
    return value


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from strings(child)


def assert_private_output(output: str):
    for value in strings(sample()):
        assert value not in output


def raw(document) -> bytes:
    return json.dumps(document, ensure_ascii=True).encode()
