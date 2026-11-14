"""Intraday source signs, publication boundaries and first prepared duplicates."""

import re
from datetime import UTC, datetime, timedelta

import pytest
from garmin_records import store_page
from identity_records import HEART, heart, store
from sqlalchemy import String, cast, event, select, update
from support.postgres_reads import only_reads
from test_garmin_normalize import DAY, NOW, garmin_heart
from test_garmin_step_validity import insert_raw_json

from app.models.garmin import (
    GarminApiResponse,
    GarminDailyProjection,
    GarminHeartMember,
    GarminHeartSet,
    GarminRawRecord,
    GarminRecordObservation,
)
from app.models.health import HeartRate, RestingHeartRate
from app.services.dashboard_views.dashboard import dashboard_data
from app.services.identities.source_precedence import Policy
from app.services.providers.garmin_normalize import build_rows, normalize_window
from app.services.providers.garmin_normalize_writing import write_rows
from app.services.providers.garmin_readers import heart_rate_samples
from app.services.readers.dashboard_queries import DashboardQuery

START = datetime.combine(DAY, datetime.min.time(), UTC)
STOP = START + timedelta(days=1)
# jsonb stores numbers as numeric: PostgreSQL archives STAMPe0 as the integer STAMP, and
# the normalizer reading that jsonb sees an integer too. Its sample then reserves the stamp.
JSONB_INTEGER = "jsonb-integer"
TABLES = (
    GarminApiResponse,
    GarminRawRecord,
    GarminRecordObservation,
    GarminHeartSet,
    GarminHeartMember,
    GarminDailyProjection,
    HeartRate,
    RestingHeartRate,
)


@pytest.mark.parametrize("projected", [False, True])
@pytest.mark.parametrize("normalized", [0, 60])
@pytest.mark.parametrize(
    "samples,negative",
    [
        ("[[STAMP, -1e-1000], [STAMP, 60]]", True),
        ('[[STAMP, "-1e-1000"], [STAMP, 60]]', True),
        ("[[STAMP, 1e30], [STAMP, -1e-1000], [STAMP, 60]]", True),
        # Beyond float and NUMERIC range on both backends; jsonb keeps it as a 401-digit
        # integer, which Python still decodes (an integer over 4300 digits is refused by
        # the client decoder and an infinity by jsonb_storable, so neither is archived).
        ("[[STAMP, -1e400], [STAMP, 60], [STAMP, -1e-1000]]", False),
        ("[[STAMP, 60], [STAMP, -1e-1000]]", False),
        ("[[STAMP.0, -1e-1000], [STAMP, 60]]", False),
        ("[[STAMPe0, -1e-1000], [STAMP, 60]]", JSONB_INTEGER),
        ('[["STAMP", -1e-1000], [STAMP, 60]]', False),
        ("[[true, -1e-1000], [999999999999999999999999, -1], [STAMP, 60]]", False),
        ("[[STAMP, 1e-1000], [STAMP, -1e-1000]]", False),
        ("[[STAMP, 0], [STAMP, -1e-1000]]", False),
        ("[[STAMP, -0e-1000], [STAMP, -1e-1000]]", False),
    ],
)
async def test_json_sample_tokens_preserve_sign_timestamps_and_prepared_choice(
    sessions, projected, normalized, samples, negative
):
    rows, _ = build_rows(
        {"heart-rate": {str(DAY): garmin_heart(DAY, [(0, normalized)])}}, DAY, DAY, "Test"
    )
    async with sessions.begin() as session:
        raw_id = await insert_raw_json(
            session,
            "heart-rate",
            '{"heartRateValues": '
            + samples.replace("STAMP", str(int(START.timestamp() * 1000)))
            + "}",
        )
        await write_rows(session, HeartRate, rows[HeartRate])
        if negative == JSONB_INTEGER:
            archived = await session.scalar(
                select(cast(GarminRawRecord.body.op("->")("heartRateValues"), String))
            )
            negative = re.match(r"\[\[\d+,", archived) is not None
            assert negative == (session.get_bind().dialect.name == "postgresql")
        if projected:
            state = GarminHeartSet(
                source_record_id=f"heart-rate:{DAY}", raw_record_id=raw_id, members_sha256="0" * 64
            )
            session.add(state)
            await session.flush()
            session.add(
                GarminHeartMember(
                    set_id=state.id, sample_key=rows[HeartRate][0]["google_data_point_id"]
                )
            )
    await assert_read(sessions, None if negative else normalized)
    if projected:
        await store_page(sessions, "heart-rate", [garmin_heart(DAY, [(0, 80)])], NOW)
        await assert_read(sessions, None if negative else normalized)


async def snapshot(session):
    return {
        model: (
            await session.execute(
                select(model.__table__).order_by(*model.__table__.primary_key.columns)
            )
        ).all()
        for model in TABLES
    }


async def assert_read(sessions, expected):
    async with sessions() as session:
        before = await snapshot(session)
        engine = session.get_bind()
        event.listen(engine, "before_cursor_execute", only_reads)
        try:
            for _ in range(2):
                assert await heart_rate_samples(session, START, STOP) == (
                    [] if expected is None else [(START, expected)]
                )
                data = await dashboard_data(session, query=DashboardQuery(DAY, 1, "UTC"))
                hourly = round(expected, 1) if expected is not None else None
                assert data["charts"]["heart_rate"] == [hourly] + [None] * 23
                sources = data["charts"]["sources"]["heart_rate"]
                assert sources["source"] == (None if expected is None else "GARMIN")
                assert sources["alternatives"] == []
        finally:
            event.remove(engine, "before_cursor_execute", only_reads)
        assert await snapshot(session) == before
        assert not session.new and not session.dirty and not session.deleted


@pytest.mark.parametrize(
    "value,expected",
    [
        (-1, None),
        (-0.25, None),
        (-0.0001, None),
        (-1e-300, None),
        ("-1e-10000", None),
        (0, 0),
        (-0.0, 0),
        ("-0e-10000", 0),
        (12.25, 12.25),
        ("12.25", 12.25),
        ("1e-10000", 0),
    ],
)
async def test_committed_signs_ignore_pending_versions_and_replay_without_writes(
    sessions, value, expected
):
    previous = None
    for index, source in enumerate((60, value, value, 60)):
        await store_page(
            sessions,
            "heart-rate",
            [garmin_heart(DAY, [(0, source)])],
            NOW + timedelta(minutes=index),
        )
        await assert_read(sessions, previous)
        async with sessions.begin() as session:
            await normalize_window(session, DAY, DAY)
        previous = expected if index in (1, 2) else 60
        await assert_read(sessions, previous)
        async with sessions() as session:
            before = await snapshot(session)
        async with sessions.begin() as session:
            stats = await normalize_window(session, DAY, DAY)
            assert stats.inserted == stats.updated == 0
            assert await snapshot(session) == before
    async with sessions() as session:
        saved = await snapshot(session)
        assert len(saved[GarminRawRecord]) == 2
        assert len(saved[GarminRecordObservation]) == 4
        assert len(saved[GarminHeartSet]) == 3
        assert len(saved[HeartRate]) == 1


@pytest.mark.parametrize("version", ["v1", "v2", "mixed"])
@pytest.mark.parametrize("observed", [False, True])
@pytest.mark.parametrize("negative", [-1, -0.25, -0.0001, -1e-300, "-1e-10000"])
async def test_legacy_inputs_keep_version_and_observation_selection(
    sessions, version, observed, negative
):
    rows, _ = build_rows({"heart-rate": {str(DAY): garmin_heart(DAY, [(0, 60)])}}, DAY, DAY, "Test")
    async with sessions.begin() as session:
        for selected in ("v1", "v2") if version == "mixed" else (version,):
            values = dict(rows[HeartRate][0])
            if selected == "v1":
                values["google_data_point_id"] = values["google_data_point_id"].removesuffix(":v2")
                if version == "mixed":
                    values["bpm"] = 90  # v2 must still win, even when it becomes invalid.
            await write_rows(session, HeartRate, [values])
    await assert_read(sessions, 60)
    for index, value in enumerate((60, negative, 60, negative)):
        body = garmin_heart(DAY, [(0, value)])
        if observed:
            await store_page(sessions, "heart-rate", [body], NOW + timedelta(minutes=index))
        elif index < 2:
            async with sessions.begin() as session:
                session.add(
                    GarminRawRecord(
                        kind="heart-rate",
                        key=str(DAY),
                        body=body,
                        body_sha256=str(index) * 64,
                        first_seen_at=NOW + timedelta(minutes=index),
                    )
                )
        await assert_read(sessions, None if index % 2 or (not observed and index > 1) else 60)
    async with sessions() as session:
        assert (await session.scalars(select(GarminHeartSet))).all() == []


@pytest.mark.parametrize("version", ["v1", "v2", "mixed"])
async def test_normalized_only_negative_is_not_rescued_by_a_positive_v1(sessions, version):
    rows, _ = build_rows({"heart-rate": {str(DAY): garmin_heart(DAY, [(0, -1)])}}, DAY, DAY, "Test")
    async with sessions.begin() as session:
        for selected in ("v1", "v2") if version == "mixed" else (version,):
            values = dict(rows[HeartRate][0])
            if selected == "v1":
                values["google_data_point_id"] = values["google_data_point_id"].removesuffix(":v2")
                if version == "mixed":
                    values["bpm"] = 60
            await write_rows(session, HeartRate, [values])
    await assert_read(sessions, None)


@pytest.mark.parametrize(
    "values,expected",
    [
        ([-1, 60], None),
        ([-0.0001, 60], None),
        (["-1e-10000", 60], None),
        ([60, -1], 60),
        (["bad", -0.0001, 60], None),
        ([10**30, "-1e-10000", 60], None),
        (["-1e10000", 60], 60),
        ([None, "NaN", 10**30, 60, -1], 60),
        (["-0e-10000", 60], 0),
    ],
)
@pytest.mark.parametrize("legacy", [False, True])
async def test_duplicates_use_first_prepared_source_sign(sessions, values, expected, legacy):
    await store_page(sessions, "heart-rate", [garmin_heart(DAY, [(0, v) for v in values])], NOW)
    async with sessions.begin() as session:
        if legacy:
            # Old positive projection, before membership evidence existed.
            rows, _ = build_rows(
                {"heart-rate": {str(DAY): garmin_heart(DAY, [(0, 60)])}}, DAY, DAY, "Test"
            )
            await write_rows(session, HeartRate, rows[HeartRate])
        else:
            await normalize_window(session, DAY, DAY)
    # For legacy positive zero projections, the source check only excludes negatives.
    await assert_read(sessions, 60 if legacy and expected == 0 else expected)


@pytest.mark.parametrize("garmin_first", [False, True])
async def test_negative_samples_cannot_block_google_or_appear_as_alternatives(
    sessions, garmin_first
):
    await store(sessions, HEART, heart(75, START, name="Synthetic"))
    policy = Policy(health=("GARMIN", "FITBIT") if garmin_first else ("FITBIT", "GARMIN"))
    for index, value in enumerate((60, "-1e-10000", 60)):
        await store_page(
            sessions,
            "heart-rate",
            [garmin_heart(DAY, [(0, value)])],
            NOW + timedelta(minutes=index),
        )
        async with sessions.begin() as session:
            await normalize_window(session, DAY, DAY)
        async with sessions() as session:
            before = await snapshot(session)
            data = await dashboard_data(session, query=DashboardQuery(DAY, 1, "UTC"), policy=policy)
            source = data["charts"]["sources"]["heart_rate"]
            if index == 1:
                assert data["charts"]["heart_rate"][0] == 75
                assert source["source"] == "FITBIT" and source["alternatives"] == []
            else:
                assert data["charts"]["heart_rate"][0] == (60 if garmin_first else 75)
                assert len(source["alternatives"]) == 1
            assert await snapshot(session) == before


async def test_empty_publication_pending_return_and_quarantine_are_preserved(sessions):
    for index, values in enumerate(([60], [-0.0001], [], [60], ["-1e-10000"], [60])):
        await store_page(
            sessions,
            "heart-rate",
            [garmin_heart(DAY, [(0, v) for v in values])],
            NOW + timedelta(minutes=index),
        )
        # In particular, an empty committed set cannot be undone by archiving a return.
        await assert_read(sessions, 60 if index == 1 else None)
        async with sessions.begin() as session:
            await normalize_window(session, DAY, DAY)
            if index == 3:
                await session.execute(
                    update(HeartRate).values(
                        is_quarantined=True, quarantine_reason="Synthetic exclusion"
                    )
                )
        await assert_read(sessions, 60 if index == 0 else None)
    async with sessions() as session:
        saved = (await session.scalars(select(HeartRate))).one()
        assert saved.bpm == 60 and saved.is_quarantined
        assert saved.quarantine_reason == "Synthetic exclusion"


async def test_negative_sign_and_membership_roll_back_together(sessions):
    for index, value in enumerate((60, "-1e-10000")):
        await store_page(
            sessions,
            "heart-rate",
            [garmin_heart(DAY, [(0, value)])],
            NOW + timedelta(minutes=index),
        )
        if index == 0:
            async with sessions.begin() as session:
                await normalize_window(session, DAY, DAY)
    await assert_read(sessions, 60)
    with pytest.raises(RuntimeError, match="synthetic rollback"):
        async with sessions.begin() as session:
            await normalize_window(session, DAY, DAY)
            assert await heart_rate_samples(session, START, STOP) == []
            raise RuntimeError("synthetic rollback")
    await assert_read(sessions, 60)


@pytest.mark.parametrize("seed", [7, 739, 2026])
async def test_raw_inputs_and_values_match_independent_legacy_query(sessions, monkeypatch, seed):
    from collections import Counter

    from garmin_heart_archive import START, STOP, grow_archive
    from garmin_heart_reference import raw_samples

    from app.core.timeutil import as_utc
    from app.services.providers import garmin_heart_values as reader

    await grow_archive(sessions, 0, 36, seed=seed)
    async with sessions() as session:
        before = await snapshot(session)
        stream = session.stream
        queries = []

        async def capture(statement, **kwargs):
            queries.append(statement.execution_options(yield_per=None, stream_results=False))
            return await stream(statement, **kwargs)

        monkeypatch.setattr(session, "stream", capture)
        for start, stop in (
            (START, STOP),
            (START + timedelta(days=12), START + timedelta(days=24)),
        ):
            queries.clear()
            actual = await reader.heart_values(session, start, stop, "GARMIN")
            assert len(queries) == 2
            old = (await session.execute(raw_samples(queries[0]))).all()
            new = (await session.execute(queries[1])).all()
            assert Counter(new) == Counter(old) and new
            # Independent SQL resolution, unchanged sign/rounding policy.
            expected = {
                key: (as_utc(moment), float(bpm))
                for key, moment, bpm in (await session.execute(queries[0])).all()
                if bpm.is_finite() and bpm >= 0
            }
            for key, samples in old:
                for negative in reader._negative_keys(key, samples):
                    expected.pop(negative, None)
                    expected.pop(f"{negative}:v2", None)
            assert Counter(actual) == Counter(expected.values()) and actual
            assert await snapshot(session) == before
        assert not session.new and not session.dirty and not session.deleted
