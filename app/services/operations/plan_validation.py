"""Bounded JSON parsing and value-free diagnostics for private plan files and state."""

import hashlib
import json
from typing import Any

from pydantic import ValidationError

from app.schemas.plan import MAX_DOCUMENT_BYTES, Content, PlanDocument, Shopping


class InvalidPlan(ValueError):
    def __init__(self, path: str, rule: str) -> None:
        self.path, self.rule = path, rule
        super().__init__(f"{path}: {rule}")


def validation_error(error: ValidationError) -> InvalidPlan:
    item = error.errors(include_input=False, include_context=False, include_url=False)[0]
    parts = list(item["loc"])
    if item["type"] == "extra_forbidden":
        parts[-1] = "?"  # Unknown field names can themselves contain private text.
    path = "$" + "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in parts)
    return InvalidPlan(path, item["type"])


def unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidPlan("$", "duplicate_key")
        result[key] = value
    return result


def parse_content[T: Content](raw: bytes, model: type[T]) -> tuple[dict, T]:
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise InvalidPlan("$", "document_too_large")
    try:
        document = json.loads(raw, object_pairs_hook=unique_object)
        parsed = model.model_validate(document)
    except ValidationError as exc:
        raise validation_error(exc) from None
    except (ValueError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, InvalidPlan):
            raise
        raise InvalidPlan("$", "invalid_json") from None
    return document, parsed


def _unique(values: list, path: str) -> None:
    if len(values) != len(set(values)):
        raise InvalidPlan(path, "duplicate_identifier")


def shopping_ids(shopping: Shopping) -> list[str]:
    return [item.id for group in shopping.groups for item in group.items] + [
        item.id for item in shopping.one_off
    ]


def validate_document(document: dict) -> PlanDocument:
    try:
        plan = PlanDocument.model_validate(document)
    except ValidationError as exc:
        raise validation_error(exc) from None
    _unique([day.id for day in plan.day_types], "$.day_types")
    _unique(shopping_ids(plan.shopping), "$.shopping")
    _unique([s.id for s in plan.training.sessions], "$.training.sessions")
    days = {day.id for day in plan.day_types}
    for i, day in enumerate(plan.day_types):
        _unique(day.weekdays, f"$.day_types[{i}].weekdays")
    for i, notes in enumerate(plan.notes):
        if notes.day_type_id not in days:
            raise InvalidPlan(f"$.notes[{i}].day_type_id", "unknown_identifier")
    sessions = {s.id for s in plan.training.sessions}
    schedule = plan.training.weekly_schedule
    _unique([row.weekday for row in schedule], "$.training.weekly_schedule")
    for i, row in enumerate(schedule):
        _unique(row.session_ids, f"$.training.weekly_schedule[{i}].session_ids")
        if set(row.session_ids) - sessions:
            raise InvalidPlan(f"$.training.weekly_schedule[{i}].session_ids", "unknown_identifier")
    for i, progression in enumerate(plan.training.progressions):
        for j, row in enumerate(progression.rows):
            if row.week_end < row.week_start:
                raise InvalidPlan(f"$.training.progressions[{i}].rows[{j}]", "week_range")
    items = [
        (f"$.shopping.groups[{i}].items[{j}]", item)
        for i, group in enumerate(plan.shopping.groups)
        for j, item in enumerate(group.items)
    ] + [(f"$.shopping.one_off[{i}]", item) for i, item in enumerate(plan.shopping.one_off)]
    for path, item in items:
        if (item.estimated_price is None) != (item.currency is None):
            raise InvalidPlan(path, "price_currency_pair")
    return plan


def load_document(raw: bytes) -> dict:
    document, _ = parse_content(raw, PlanDocument)
    validate_document(document)
    return document


def content_digest(document: dict) -> str:
    # Section order is meaningful; nested object key order and whitespace are not.
    content = [document["schema"], [(k, v) for k, v in document.items() if k != "schema"]]
    encoded = json.dumps(content, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


def section_counts(document: dict) -> dict[str, int]:
    plan = validate_document(document)
    return {
        "sections": len(document) - 1,
        "targets": len(plan.targets.cards),
        "day_types": len(plan.day_types),
        "meals": sum(len(day.meals) for day in plan.day_types),
        "notes": sum(len(note.items) for note in plan.notes),
        "recipes": len(plan.batch_cooking.recipes),
        "shopping_items": len(shopping_ids(plan.shopping)),
        "sessions": len(plan.training.sessions),
        "progressions": len(plan.training.progressions),
        "measurements": len(plan.measurements),
        "assumptions": len(plan.assumptions),
    }
