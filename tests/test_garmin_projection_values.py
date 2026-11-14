"""Multiset equivalence against independent pre-fix queries on both SQL backends."""

import random
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from garmin_projection_archive import payload
from garmin_projection_reference import scalar_query, sleep_query, step_query
from sqlalchemy import insert

from app.models.garmin import GarminApiResponse, GarminDailyProjection, GarminRawRecord
from app.models.garmin import GarminRecordObservation as Observation
from app.models.health import SleepSession, Steps
from app.services.providers import garmin_daily_values as scalars
from app.services.providers import garmin_sleep_values as sleep
from app.services.providers import garmin_steps as steps
from app.services.providers.garmin_normalize import build_rows

START, SEEN = date(2000, 1, 1), datetime(2026, 1, 1, tzinfo=UTC)
METRICS = ("steps", *scalars.SCALARS, "sleep")


def definition(metric):
    if metric == "sleep":
        return (
            SleepSession,
            SleepSession.duration_seconds,
            "sleep",
            (("sleep", ("sleepTimeSeconds",)),),
        )
    if metric == "steps":
        return Steps, Steps.count, "steps", (("steps", ("totalSteps",)),)
    return scalars.SCALARS[metric]


async def seed_archive(session, metric, days, seed=None):
    """Seed all legacy/publication cases; scale archives have one committed row per day."""
    model, column, prefix, inputs = definition(metric)
    raw_kind, path = inputs[0]
    rng = random.Random(seed)
    raw_rows, normalized, projections, observations = [], [], [], []
    session.add_all(
        GarminApiResponse(
            id=response,
            fetched_at=SEEN,
            kind=raw_kind,
            method="GET",
            path="/synthetic",
            request_sha256="0" * 64,
            status_code=200,
            body=b"[]",
            body_sha256="0" * 64,
            body_size=2,
        )
        for response in (1, 2, 3)
    )
    await session.flush()
    for offset in range(days):
        day = START + timedelta(days=offset)
        case = offset % 14
        if case == 0:  # Empty day.
            continue
        body = payload(raw_kind, path, day, 42)
        built, _ = build_rows({raw_kind: {str(day): body}}, day, day, "Synthetic")
        row = built[model][0]
        row[column.key] = rng.choice((0, 42, -0.0001)) if seed is not None else 42
        row["is_quarantined"] = case == 9
        if case in (12, 13):  # Legacy rows with a foreign prefix or no daily identity.
            row["source_record_id"] = prefix.upper() + ":" + str(day) if case == 12 else None
        normalized.append(row)
        if case == 1:  # Normalized-only archive.
            continue
        ids = [2 * offset + 1, 2 * offset + 2]
        for version, identifier in enumerate(ids if seed is not None else ids[:1]):
            value = rng.choice((-1, "-1e-10000", "broken", "NaN", None)) if version else 42
            if case == 12 and version:
                value = -1
            changed = payload(raw_kind, path, day, 42)
            target = changed
            for name in path[:-1]:
                target = target[name]
            target[path[-1]] = value
            raw_rows.append(
                dict(
                    id=identifier,
                    kind=raw_kind,
                    key=str(day),
                    body=changed,
                    body_sha256=f"{identifier:064x}",
                    first_seen_at=SEEN,
                )
            )
        if case in (2, 12, 13):  # Legacy max-id fallback.
            continue
        # Equal timestamps: observation id breaks ties; A -> B -> A reuses raw A.
        observed = ids + ids[:1] if seed is not None else ids[:1]
        observations.extend(
            dict(raw_record_id=i, response_id=response, observed_at=SEEN)
            for response, i in enumerate(observed, 1)
        )
        if case == 3:  # Observed legacy without projection evidence.
            continue
        selected = {4: ids[:1], 5: ids, 6: ids + ids[:1]}.get(case, ids[:1])
        for identifier in selected:
            projections.append(
                dict(
                    source_record_id=row["source_record_id"],
                    raw_record_ids={} if case == 11 else {raw_kind: identifier},
                    normalized_key=None if case == 7 else row["google_data_point_id"],
                )
            )
        if case == 10:  # An old normalized version alongside v2.
            normalized.append(
                {**row, "google_data_point_id": row["google_data_point_id"].removesuffix(":v2")}
            )
    for table, rows in (
        (GarminRawRecord, raw_rows),
        (model, normalized),
        (Observation, observations),
        (GarminDailyProjection, projections),
    ):
        if rows:
            await session.execute(insert(table), rows)


async def query(metric, first, last, reference=False):
    if metric == "sleep":
        return (sleep_query if reference else sleep._query)(
            datetime.combine(first, datetime.min.time(), UTC),
            datetime.combine(last, datetime.min.time(), UTC),
            "GARMIN",
        )
    if metric != "steps":
        return (scalar_query if reference else scalars._query)(metric, first, last, "GARMIN")
    if reference:
        return step_query(first, last)
    session = AsyncMock()
    session.stream.side_effect = LookupError
    with pytest.raises(LookupError):
        await anext(steps.step_values(session, first, last))
    return session.stream.call_args.args[0].execution_options(yield_per=None, stream_results=False)


@pytest.mark.parametrize("metric", METRICS)
@pytest.mark.parametrize("seed", (11, 732, 2026))
async def test_rows_match_independent_old_query(sessions, metric, seed):
    async with sessions.begin() as session:
        await seed_archive(session, metric, 72, seed)
    async with sessions() as session:
        for first, last in (
            (date.min, SEEN.date()),
            (START + timedelta(days=40), START + timedelta(days=69)),
        ):
            expected = (await session.execute(await query(metric, first, last, True))).all()
            actual = (await session.execute(await query(metric, first, last))).all()
            # ORM sleep rows stay in the identity map; every other selected field is scalar.
            # A multiset retains missing, extra and duplicated rows without imposing order.
            assert Counter(actual) == Counter(expected)
            assert actual
