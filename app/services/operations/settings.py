"""Appearance choices and display preferences shared by every browser that opens the dashboard.

Only preset ids are stored for appearance. Uploaded images (logo, Umay portrait,
background) stay in the browser that uploaded them (IndexedDB); the server keeps just the
`upload` marker, and a browser without that upload falls back to the default preset.

Display preferences (time zone choice, body model rotation) carry a `schema_version`, so a
row written in an older shape is never misread and a newer row is never overwritten.
"""

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timeutil import as_utc
from app.db.dialect import dialect_insert
from app.models.settings import AppearanceSettings, DisplayPreferences
from app.schemas.settings import Appearance, Display

SINGLETON_ID = 1
# Same rule as the preset ids in frontend/src/appearance.js; also matches `upload`.
PresetId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,47}$")]
# Shape of `display_preferences.preferences`. Bump it when the shape changes and upgrade
# older rows in _display_response(); a row newer than this server is left untouched.
DISPLAY_SCHEMA_VERSION = 1
# `auto` (each device's own zone) or an IANA-style name. Each browser checks the name with
# Intl and falls back to `auto`, so the server does not need the same zone database.
TimeZoneChoice = Annotated[
    str,
    StringConstraints(
        pattern=r"^(auto|[A-Za-z][A-Za-z0-9_+-]*(/[A-Za-z0-9_+-]+){0,2})$", max_length=64
    ),
]


class AppearanceSelection(BaseModel):
    """Chosen preset per appearance kind; a missing kind means the default preset."""

    model_config = ConfigDict(extra="forbid")

    logo: PresetId | None = None
    umay: PresetId | None = None
    background: PresetId | None = None


class DisplayPreferenceValues(BaseModel):
    """Display preferences; a missing one means the browser's default."""

    model_config = ConfigDict(extra="forbid", strict=True)

    timezone: TimeZoneChoice | None = None
    motion: bool | None = None


class DisplayPreferencesBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    preferences: DisplayPreferenceValues


async def _store(
    session: AsyncSession, model: type[AppearanceSettings | DisplayPreferences], values: dict
) -> None:
    """Atomically insert/update the singleton without ending the caller's transaction."""
    statement = dialect_insert(session, model).values(id=SINGLETON_ID, **values)
    statement = statement.on_conflict_do_update(index_elements=[model.id], set_=values)
    result = await session.scalars(
        statement.returning(model), execution_options={"populate_existing": True}
    )
    result.all()  # Refresh a singleton already loaded by this session too.


def _response(row: AppearanceSettings | None) -> Appearance:
    if row is None:
        return {"selection": {}, "updated_at": None}
    try:
        selection = AppearanceSelection.model_validate(row.selection).model_dump(exclude_none=True)
    except ValidationError:
        selection = {}  # A hand-edited row must not break every browser's dashboard.
    return {"selection": selection, "updated_at": as_utc(row.updated_at)}


async def get_appearance(session: AsyncSession) -> Appearance:
    return _response(await session.get(AppearanceSettings, SINGLETON_ID))


async def put_appearance(session: AsyncSession, body: AppearanceSelection) -> Appearance:
    selection = body.model_dump(exclude_none=True)
    now = datetime.now(UTC)
    await _store(session, AppearanceSettings, {"selection": selection, "updated_at": now})
    return {"selection": selection, "updated_at": now}


def _display_response(row: DisplayPreferences | None) -> Display:
    empty: Display = {
        "schema_version": DISPLAY_SCHEMA_VERSION,
        "preferences": {},
        "updated_at": None,
    }
    if row is None or row.schema_version != DISPLAY_SCHEMA_VERSION:
        return empty  # Nothing stored, or a newer shape this server cannot read.
    try:
        values = DisplayPreferenceValues.model_validate(row.preferences)
    except ValidationError:
        return empty  # A hand-edited row must not break every browser's dashboard.
    return {
        "schema_version": DISPLAY_SCHEMA_VERSION,
        "preferences": values.model_dump(exclude_none=True),
        "updated_at": as_utc(row.updated_at),
    }


class NewerDisplaySchema(ValueError):
    """A newer display preference row must be preserved."""


async def get_display(session: AsyncSession) -> Display:
    return _display_response(await session.get(DisplayPreferences, SINGLETON_ID))


async def put_display(session: AsyncSession, body: DisplayPreferencesBody) -> Display:
    row = await session.get(DisplayPreferences, SINGLETON_ID)
    if row is not None and row.schema_version > DISPLAY_SCHEMA_VERSION:
        raise NewerDisplaySchema("Görünüm tercihleri daha yeni bir sürümle kaydedilmiş.")
    preferences = body.preferences.model_dump(exclude_none=True)
    now = datetime.now(UTC)
    values: Display = {
        "schema_version": DISPLAY_SCHEMA_VERSION,
        "preferences": preferences,
        "updated_at": now,
    }
    await _store(session, DisplayPreferences, dict(values))
    return values
