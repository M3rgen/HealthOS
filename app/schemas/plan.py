"""Version-one content-only plan schema. All strings are rendered as plain text."""

from datetime import date, datetime
from typing import Annotated, Any, NotRequired, TypedDict

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

MAX_DOCUMENT_BYTES = 1024 * 1024
MAX_ROWS = 32
MAX_SHOPPING_ITEMS = MAX_ROWS * MAX_ROWS + MAX_ROWS


def readable_text(value: str) -> str:
    if any(ord(c) < 32 and c not in "\n\r\t" or 0xD800 <= ord(c) <= 0xDFFF for c in value):
        raise ValueError("invalid text character")
    return value


def calendar_date(value: str) -> str:
    date.fromisoformat(value)
    return value


Text = Annotated[
    str, StringConstraints(min_length=1, max_length=2000), AfterValidator(readable_text)
]
ShortText = Annotated[
    str, StringConstraints(min_length=1, max_length=200), AfterValidator(readable_text)
]
Identifier = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,47}$")]
ISODate = Annotated[
    str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}$"), AfterValidator(calendar_date)
]
Weekday = Annotated[int, Field(ge=1, le=7)]
Week = Annotated[int, Field(ge=1, le=520)]
type Rows[T] = Annotated[list[T], Field(max_length=MAX_ROWS)]


class Content(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class Meta(Content):
    title: ShortText | None = None
    subtitle: ShortText | None = None
    date: ISODate | None = None
    language: (
        Annotated[str, StringConstraints(pattern=r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8}){0,3}$")] | None
    ) = None
    warning: Text | None = None


class TargetCard(Content):
    label: ShortText
    value: ShortText
    description: Text


class Targets(Content):
    cards: Rows[TargetCard] = Field(default_factory=list)
    adjustment_rules: Rows[Text] = Field(default_factory=list)


class Meal(Content):
    label: ShortText
    name: ShortText
    content: Text
    kcal: Annotated[float, Field(ge=0, le=100000, allow_inf_nan=False)] | None = None
    protein_g: Annotated[float, Field(ge=0, le=10000, allow_inf_nan=False)] | None = None
    ingredients: Rows[ShortText] = Field(default_factory=list)
    preparation: Text | None = None


class DayType(Content):
    id: Identifier
    name: ShortText
    weekdays: Annotated[list[Weekday], Field(max_length=7)] = Field(default_factory=list)
    meals: Rows[Meal] = Field(default_factory=list)


class DayNotes(Content):
    day_type_id: Identifier
    items: Rows[Text]


class Recipe(Content):
    name: ShortText
    summary: Text
    ingredients: Rows[ShortText]
    preparation: Text
    portions: Text


class BatchCooking(Content):
    recipes: Rows[Recipe] = Field(default_factory=list)
    storage_rules: Rows[Text] = Field(default_factory=list)
    substitution_rules: Rows[Text] = Field(default_factory=list)


class ShoppingItem(Content):
    id: Identifier
    name: ShortText
    quantity: ShortText
    estimated_price: Annotated[float, Field(ge=0, le=1000000000, allow_inf_nan=False)] | None = None
    currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")] | None = None
    store: ShortText | None = None


class ShoppingGroup(Content):
    name: ShortText
    items: Rows[ShoppingItem]


class Shopping(Content):
    groups: Rows[ShoppingGroup] = Field(default_factory=list)
    one_off: Rows[ShoppingItem] = Field(default_factory=list)


class ScheduleRow(Content):
    weekday: Weekday
    content: Text
    session_ids: Rows[Identifier] = Field(default_factory=list)


class Exercise(Content):
    movement: ShortText
    sets_reps: ShortText
    note: Text | None = None


class TrainingSession(Content):
    id: Identifier
    name: ShortText
    exercises: Rows[Exercise]


class ProgressionRow(Content):
    week_start: Week
    week_end: Week
    content: Text
    duration: ShortText


class Progression(Content):
    name: ShortText
    rows: Rows[ProgressionRow]


class Training(Content):
    weekly_schedule: Annotated[list[ScheduleRow], Field(max_length=7)] = Field(default_factory=list)
    sessions: Rows[TrainingSession] = Field(default_factory=list)
    progressions: Rows[Progression] = Field(default_factory=list)
    rules: Rows[Text] = Field(default_factory=list)


class Assumption(Content):
    title: ShortText
    text: Text


class PlanDocument(Content):
    schema_version: Annotated[int, Field(ge=1, le=1, alias="schema")]
    meta: Meta = Field(default_factory=Meta)
    targets: Targets = Field(default_factory=Targets)
    day_types: Rows[DayType] = Field(default_factory=list)
    notes: Rows[DayNotes] = Field(default_factory=list)
    batch_cooking: BatchCooking = Field(default_factory=BatchCooking)
    shopping: Shopping = Field(default_factory=Shopping)
    training: Training = Field(default_factory=Training)
    measurements: Rows[Text] = Field(default_factory=list)
    assumptions: Rows[Assumption] = Field(default_factory=list)


class PlanStateBody(Content):
    start_date: ISODate | None
    checked_item_ids: Annotated[list[Identifier], Field(max_length=MAX_SHOPPING_ITEMS)]


class PlanStateView(TypedDict):
    start_date: str | None
    checked_item_ids: list[str]


class PlanView(TypedDict):
    """`plan` is the stored document as imported (section order kept), or null before import."""

    plan: dict[str, Any] | None
    version: NotRequired[int]
    schema_version: NotRequired[int]
    imported_at: NotRequired[datetime]
    state: NotRequired[PlanStateView]
