"""Table-driven rejection cases for every populated field and documented schema rule."""

from copy import deepcopy

import pytest
from support.plans import changed, raw, sample

from app.schemas.plan import MAX_DOCUMENT_BYTES, PlanDocument
from app.services.operations.plan_validation import InvalidPlan, content_digest, load_document

DOCUMENT = sample()
SCHEMA = PlanDocument.model_json_schema()


def field_rules(schema, value, path=()):
    if "$ref" in schema:
        schema = SCHEMA["$defs"][schema["$ref"].split("/")[-1]]
    if "anyOf" in schema:
        schema = next(s for s in schema["anyOf"] if s.get("type") != "null")
    kind = schema["type"]
    yield path, "type", [] if kind == "object" else {}  # Never coerced to the desired type.
    if kind == "object":
        yield path, "unknown", {**value, "private unknown field": "private value"}
        for key in schema.get("required", []):
            yield path, "required-" + key, {k: v for k, v in value.items() if k != key}
        for key, child in schema["properties"].items():
            if key in value:
                yield from field_rules(child, value[key], (*path, key))
    elif kind == "array":
        yield path, "maxItems", [deepcopy(value[0])] * (schema["maxItems"] + 1)
        if value:
            yield from field_rules(schema["items"], value[0], (*path, 0))
    elif kind == "string":
        if "minLength" in schema:
            yield path, "minLength", ""
        if "maxLength" in schema:
            yield path, "maxLength", "x" * (schema["maxLength"] + 1)
        if "pattern" in schema:
            yield path, "pattern", "!invalid!"
    elif kind in {"integer", "number"}:
        yield path, "minimum", schema["minimum"] - 1
        yield path, "maximum", schema["maximum"] + 1
        yield path, "boolean", True
        yield path, "numeric-string", str(value)
        if kind == "number":
            yield path, "nonfinite", float("inf")
        else:
            yield path, "fraction", 1.5


CASES = list(field_rules(SCHEMA, DOCUMENT))


@pytest.mark.parametrize("path,rule,value", CASES, ids=[str(p) + ":" + r for p, r, _ in CASES])
def test_every_field_rule_refuses_invalid_example(path, rule, value):
    with pytest.raises(InvalidPlan):
        load_document(raw(changed(DOCUMENT, path, value)))


@pytest.mark.parametrize(
    "path,value,expected",
    [
        (("schema",), "1", "$.schema"),
        (("meta", "date"), "2026-02-30", "$.meta.date"),
        (("meta", "title"), "private\u0000text", "$.meta.title"),
        (("meta", "title"), "private\ud800text", "$.meta.title"),
        (("meta",), None, "$.meta"),
        (("day_types", 0, "weekdays"), [1, 1], "$.day_types[0].weekdays"),
        (("day_types",), DOCUMENT["day_types"] * 2, "$.day_types"),
        (("notes", 0, "day_type_id"), "unknown", "$.notes[0].day_type_id"),
        (("shopping", "one_off", 0, "id"), "example-grain", "$.shopping"),
        (("shopping", "one_off", 0, "currency"), None, "$.shopping.one_off[0]"),
        (("shopping", "one_off", 0, "estimated_price"), None, "$.shopping.one_off[0]"),
        (("training", "sessions"), DOCUMENT["training"]["sessions"] * 2, "$.training.sessions"),
        (("training", "weekly_schedule", 1, "weekday"), 1, "$.training.weekly_schedule"),
        (
            ("training", "weekly_schedule", 0, "session_ids"),
            ["unknown"],
            "$.training.weekly_schedule[0].session_ids",
        ),
        (
            ("training", "weekly_schedule", 0, "session_ids"),
            ["example-session"] * 2,
            "$.training.weekly_schedule[0].session_ids",
        ),
        (
            ("training", "progressions", 0, "rows", 0, "week_start"),
            3,
            "$.training.progressions[0].rows[0]",
        ),
    ],
)
def test_semantic_rules_and_safe_paths(path, value, expected):
    with pytest.raises(InvalidPlan) as error:
        load_document(raw(changed(DOCUMENT, path, value)))
    assert error.value.path == expected
    assert "private" not in str(error.value)


@pytest.mark.parametrize(
    "payload,rule",
    [
        (b'{"schema":1,"schema":1}', "duplicate_key"),
        (b'{"schema":1,"meta":{"title":"a","title":"b"}}', "duplicate_key"),
        (b'{"schema":1,', "invalid_json"),
        (b"\xff", "invalid_json"),
        (b"[" * 2000, "invalid_json"),
        (b" " * (MAX_DOCUMENT_BYTES + 1), "document_too_large"),
    ],
)
def test_file_level_rules(payload, rule):
    with pytest.raises(InvalidPlan) as error:
        load_document(payload)
    assert error.value.rule == rule


def test_example_minimal_sections_plain_text_and_order_survive():
    assert load_document(raw(DOCUMENT)) == DOCUMENT
    assert load_document(b'{"schema":1}') == {"schema": 1}
    for section in DOCUMENT.keys() - {"schema"}:
        # Independent optional sections; notes must bring their referenced day type.
        subset = {"schema": 1, section: DOCUMENT[section]}
        if section == "notes":
            subset["day_types"] = DOCUMENT["day_types"]
        assert load_document(raw(subset)) == subset
    literal = "<b>Örnek</b> **literal**\n\ttext"
    plan = changed(DOCUMENT, ("meta", "title"), literal)
    assert load_document(raw(plan))["meta"]["title"] == literal
    reordered = dict(reversed(list(DOCUMENT.items())))
    assert list(load_document(raw(reordered))) == list(reordered)
    assert content_digest(reordered) != content_digest(DOCUMENT)
    nested = deepcopy(DOCUMENT)
    nested["meta"] = dict(reversed(list(nested["meta"].items())))
    assert content_digest(nested) == content_digest(DOCUMENT)
