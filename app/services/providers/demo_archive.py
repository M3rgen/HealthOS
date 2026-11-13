"""Load, describe and remove the synthetic sample data (app/services/demo_data.py).

The sample lives in the shared normalized tables only, as provider ``DEMO`` with
``demo:<kind>:<key>`` record keys (app/core/providers.py). It never enters a raw archive
(``raw_health_data``, ``garmin_raw_records``, the manual tables): those keep what a provider
returned or the owner typed, they are append-only, and no provider returned these rows. So the
archive's guards never have to bend to take the sample back.

* Loading refuses an archive that already holds device data (a Google or Garmin raw record, or
  a normalized row of any other provider): the sample is a way to look around an empty
  installation, never a layer over real records. Loading again adds only the days not yet
  present (``ON CONFLICT DO NOTHING`` on the record key); manual entries are left alone.
* Removing deletes exactly the rows whose provider is ``DEMO`` *and* whose key is a sample key,
  in the sample's tables only. It refuses, deleting nothing, when any row carries one of the two
  marks without the other: such a row was not written here and is not the sample's to delete.

Callers own the transaction (the CLI command and the API route commit).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, delete, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.providers import DEMO, DEMO_KEY_PREFIX
from app.db.dialect import dialect_insert
from app.models.garmin import GarminRawRecord
from app.models.health import Steps
from app.models.state import RawHealthData
from app.schemas.demo import SampleStatus
from app.services.providers.demo_data import TABLES, generate

CHUNK = 1000


class SampleRefused(Exception):
    """The archive is not in a state the sample may be loaded into or removed from."""


def _sample(model: Any):
    return and_(model.provider == DEMO, model.google_data_point_id.startswith(DEMO_KEY_PREFIX))


def _other(model: Any):
    return model.provider != DEMO


def _mixed(model: Any):
    """A row with one sample mark but not the other."""
    marked = model.google_data_point_id.startswith(DEMO_KEY_PREFIX)
    return or_(and_(model.provider == DEMO, ~marked), and_(model.provider != DEMO, marked))


async def _device_data(session: AsyncSession) -> bool:
    for statement in (
        select(exists().where(RawHealthData.id.is_not(None))),
        select(exists().where(GarminRawRecord.id.is_not(None))),
        *(select(exists().where(_other(model))) for model in TABLES),
    ):
        if await session.scalar(statement):
            return True
    return False


async def sample_status(session: AsyncSession) -> SampleStatus:
    """Whether the sample is loaded, its row count and the days it covers."""
    rows = 0
    for model in TABLES:
        rows += await session.scalar(select(func.count()).where(_sample(model))) or 0
    first, last = (
        await session.execute(
            select(func.min(Steps.source_record_id), func.max(Steps.source_record_id)).where(
                _sample(Steps)
            )
        )
    ).one()
    return {
        "loaded": rows > 0,
        "rows": rows,
        "first_day": date.fromisoformat(first.split(":", 1)[1]) if first else None,
        "last_day": date.fromisoformat(last.split(":", 1)[1]) if last else None,
    }


async def load_sample(session: AsyncSession, zone: ZoneInfo, now: datetime) -> dict[str, int]:
    """Write the sample for the days up to ``now``'s day in ``zone``; per table the rows added."""
    if await _device_data(session):
        raise SampleRefused("Arşivde gerçek veri var; örnek veri yalnız boş bir kuruluma yüklenir.")
    sample = generate(now.astimezone(zone).date(), zone, now)
    added: dict[str, int] = {}
    for model in TABLES:
        rows = sample.rows.get(model, [])
        table = model.__table__
        count = 0
        for offset in range(0, len(rows), CHUNK):
            statement = (
                dialect_insert(session, table)
                .values(rows[offset : offset + CHUNK])
                .on_conflict_do_nothing(index_elements=["google_data_point_id"])
            )
            result = await session.execute(statement)
            count += max(getattr(result, "rowcount", 0) or 0, 0)
        added[model.__tablename__] = count
    return added


async def remove_sample(session: AsyncSession) -> dict[str, int]:
    """Delete the sample's rows; per table the rows deleted. Refuses (deleting nothing) when
    a row in the sample's tables is marked as sample data in one way but not the other."""
    for model in TABLES:
        if await session.scalar(select(exists().where(_mixed(model)))):
            raise SampleRefused(
                f"{model.__tablename__}: örnek veri işareti tutarsız bir kayıt var; "
                "hiçbir şey silinmedi."
            )
    removed: dict[str, int] = {}
    for model in TABLES:
        result = await session.execute(
            delete(model).where(_sample(model)).execution_options(synchronize_session=False)
        )
        removed[model.__tablename__] = max(getattr(result, "rowcount", 0) or 0, 0)
    return removed
