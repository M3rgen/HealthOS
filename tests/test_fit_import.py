"""Real ASGI upload -> guarded archive -> identity -> shared Garmin readers, synthetic only."""

import base64
import struct
from datetime import timedelta
from hashlib import sha256
from pathlib import Path

import httpx
import pytest
from fit_factory import START, activity, activity_messages, definition, fit_file
from sqlalchemy import func, select, update

from app import main
from app.core.config import Settings
from app.core.fit import FIT_EXERCISE, FIT_FILE, FIT_HEART_RATE
from app.db.session import get_session
from app.models.health import ExerciseSession, HeartRate
from app.models.identity_tables import RawHealthDataIdentity, RawSourceSighting
from app.models.raw_versions import RawHealthDataVersion
from app.models.state import RawHealthData
from app.services.identities.identity_backfill import identity_backfill
from app.services.providers.fit_import import import_fit
from app.services.providers.fit_records import parse_fit
from app.services.providers.garmin_readers import heart_rate_samples, workouts

PATH = "/api/imports/garmin-fit"
HEADERS = {"Content-Type": "application/octet-stream", "X-HealthOS-Request": "1"}
FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_activity.fit"


@pytest.fixture
async def client(sessions, monkeypatch):
    settings = Settings(
        _env_file=None,
        healthos_sources="none",
        app_ui_token="synthetic-fit-test-ui-token-varied-characters",
    )
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    application = main.create_app()

    async def session_dep():
        async with sessions() as session:
            yield session

    application.dependency_overrides[get_session] = session_dep
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(application),
        base_url="http://localhost",
        headers={"Authorization": f"Bearer {settings.app_ui_token}"},
    ) as http:
        yield http, application
    await application.state.runtime.shutdown()


async def count(session, model):
    return await session.scalar(select(func.count()).select_from(model))


async def test_synthetic_fit_end_to_end_and_reupload(client, sessions):
    http, application = client
    data = FIXTURE.read_bytes()
    first = await http.post(PATH, content=data, headers=HEADERS)
    assert first.status_code == 200, first.text
    assert first.headers["cache-control"] == "no-store"
    assert first.json() == {
        "sha256": sha256(data).hexdigest(),
        "records": 3,
        "inserted": 3,
        "updated": 0,
        "unchanged": 0,
    }
    second = await http.post(PATH, content=data, headers=HEADERS)
    assert second.json() == {**first.json(), "inserted": 0, "unchanged": 3}
    assert application.state.runtime.garmin is None  # no key or live API dependency
    async with sessions() as session:
        assert await count(session, RawHealthData) == 4
        assert await count(session, ExerciseSession) == 1
        assert await count(session, HeartRate) == 2
        assert await count(session, RawHealthDataVersion) == 0
        assert await count(session, RawSourceSighting) == 2
        file = await session.scalar(
            select(RawHealthData).where(RawHealthData.data_type == FIT_FILE.name)
        )
        assert base64.b64decode(file.raw_json[FIT_FILE.json_key]["body"]) == data
        assert file.payload_source == "garmin-fit"
        rows = await workouts(session, START, START + timedelta(hours=1))
        assert len(rows) == 1
        row = rows[0]
        assert row["provider"] == row["source_code"] == "GARMIN"
        assert row["exercise_type"] == "RUNNING"
        assert row["start_time"] == START
        assert row["end_time"] == START + timedelta(seconds=600)
        assert row["duration_seconds"] == 590.5
        assert row["distance_meters"] == 2500
        assert row["calories_kcal"] == 150
        assert await heart_rate_samples(session, START, START + timedelta(minutes=10)) == [
            (START, 120),
            (START + timedelta(seconds=1), 121),
        ]
        keys = (
            await session.scalars(
                select(RawHealthData.google_data_point_id).where(
                    RawHealthData.data_type != FIT_FILE.name
                )
            )
        ).all()
        assert all(key.startswith("synthetic:v2s:") for key in keys)


async def test_file_encoding_changes_reuse_the_same_record_identities(client, sessions):
    http, _ = client
    first = await http.post(PATH, content=FIXTURE.read_bytes(), headers=HEADERS)
    # Big endian + developer fields changes the bytes, not the activity or sample values.
    second = await http.post(PATH, content=activity(endian=">", developer=True), headers=HEADERS)
    assert second.status_code == 200, second.text
    assert second.json()["sha256"] != first.json()["sha256"]
    assert second.json()["inserted"] == 0 and second.json()["unchanged"] == 3
    async with sessions() as session:
        assert await count(session, RawHealthData) == 5  # two original files, three records
        assert await count(session, RawHealthDataVersion) == 0
    # Existing reconciler accepts these namespaced RecordTypes unchanged.
    results = await identity_backfill(sessions)
    assert FIT_EXERCISE.name in results and FIT_HEART_RATE.name in results
    assert all(not stats.error for stats in results.values())


async def test_correction_keeps_previous_file_and_payload(client, sessions):
    http, _ = client
    await http.post(PATH, content=activity(), headers=HEADERS)
    changed = activity(bpm=130)
    response = await http.post(PATH, content=changed, headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["updated"] == 2 and response.json()["unchanged"] == 1
    async with sessions() as session:
        assert await count(session, RawHealthData) == 5
        assert await count(session, RawHealthDataVersion) == 2
        old = (await session.scalars(select(RawHealthDataVersion))).all()
        assert {r.raw_json[FIT_HEART_RATE.json_key]["beatsPerMinute"] for r in old} == {120, 121}
        assert [
            bpm
            for _, bpm in await heart_rate_samples(session, START, START + timedelta(minutes=10))
        ] == [130, 131]


async def test_two_devices_are_never_collapsed(client, sessions):
    http, _ = client
    await http.post(PATH, content=activity(), headers=HEADERS)
    response = await http.post(PATH, content=activity(serial=4243), headers=HEADERS)
    assert response.json()["inserted"] == 3
    async with sessions() as session:
        assert len(await workouts(session, START, START + timedelta(hours=1))) == 2
        assert len(await heart_rate_samples(session, START, START + timedelta(minutes=10))) == 4


async def test_failed_import_rolls_back_file_and_records(sessions, monkeypatch):
    import app.services.providers.fit_import as service

    original = service.store_records

    async def fail_after_session(session, spec, records, **kwargs):
        result = await original(session, spec, records, **kwargs)
        if spec == FIT_HEART_RATE:
            raise RuntimeError("synthetic write failure")
        return result

    monkeypatch.setattr(service, "store_records", fail_after_session)
    with pytest.raises(RuntimeError, match="synthetic write failure"):
        async with sessions.begin() as session:
            await import_fit(session, parse_fit(activity()))
    async with sessions() as session:
        for model in (RawHealthData, ExerciseSession, HeartRate, RawSourceSighting):
            assert await count(session, model) == 0


async def test_quarantine_and_deletion_marks_are_respected_by_fit_readers(client, sessions):
    http, _ = client
    await http.post(PATH, content=activity(), headers=HEADERS)
    async with sessions.begin() as session:
        await session.execute(
            update(RawHealthData)
            .where(RawHealthData.data_type == FIT_EXERCISE.name)
            .values(is_quarantined=True)
        )
        await session.execute(
            update(RawHealthData)
            .where(RawHealthData.data_type == FIT_HEART_RATE.name)
            .values(source_deleted_at=START)
        )
    async with sessions() as session:
        assert await workouts(session, START, START + timedelta(hours=1)) == []
        assert await heart_rate_samples(session, START, START + timedelta(minutes=10)) == []


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"not-fit",
        activity()[:-1],
        fit_file(activity_messages() + b"\x04"),
        fit_file(activity_messages() + b"\x02\x00"),
    ],
)
async def test_damaged_or_undefined_message_never_partially_imports(client, sessions, data):
    http, _ = client
    response = await http.post(PATH, content=data, headers=HEADERS)
    assert response.status_code == 422
    async with sessions() as session:
        assert await count(session, RawHealthData) == 0


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("endian", ["<", ">"])
@pytest.mark.parametrize(
    "kind, field, code, value",
    [
        (20, (3, 1, 1), "b", -1),  # signed heart rate
        (20, (3, 2, 2), "H", 120),  # wrong uint8 width / array
        (18, (9, 4, 0x85), "i", -1),  # signed distance
        (18, (7, 4, 0x88), "f", 1.0),  # floating-point elapsed time
        (18, (8, 8, 0x86), "II", (1000, 2000)),  # timer array
        (0, (3, 4, 0x86), "I", 0),  # uint32 instead of uint32z serial
    ],
)
async def test_wrong_field_type_at_eof_never_writes_or_changes_rows(
    client, sessions, existing, endian, kind, field, code, value
):
    http, _ = client
    if existing:
        assert (await http.post(PATH, content=activity(), headers=HEADERS)).status_code == 200
    models = (
        RawHealthData,
        RawHealthDataVersion,
        ExerciseSession,
        HeartRate,
        RawHealthDataIdentity,
        RawSourceSighting,
    )

    async def snapshot():
        async with sessions() as session:
            return [
                (
                    await session.execute(
                        select(model.__table__).order_by(*model.__table__.primary_key.columns)
                    )
                )
                .mappings()
                .all()
                for model in models
            ]

    before = await snapshot()
    # A complete valid prefix would change existing samples if parsing wrote incrementally.
    data = activity_messages(bpm=130, endian=endian)
    data += definition(3, kind, [field], endian=endian)
    data += b"\x03" + struct.pack(endian + code, *(value if isinstance(value, tuple) else (value,)))
    response = await http.post(PATH, content=fit_file(data), headers=HEADERS)
    assert response.status_code == 422
    assert response.json() == {"detail": "FIT alanının türü veya skaler boyutu geçersiz."}
    assert await snapshot() == before


async def test_auth_csrf_type_size_and_rate_budgets(client, sessions, monkeypatch):
    from app.core.request_limits import RequestBudget

    http, application = client
    assert (
        await http.post(
            PATH, content=activity(), headers={**HEADERS, "Authorization": "Bearer wrong"}
        )
    ).status_code == 401
    assert (await http.post(PATH, content=activity())).status_code == 403
    assert (
        await http.post(
            PATH, content=activity(), headers={**HEADERS, "Origin": "https://example.invalid"}
        )
    ).status_code == 403
    assert (
        await http.post(
            PATH, content=activity(), headers={**HEADERS, "Content-Type": "multipart/form-data"}
        )
    ).status_code == 415
    import app.api.fit_import as api

    monkeypatch.setattr(api, "MAX_FIT_BYTES", 64)
    assert (await http.post(PATH, content=activity(), headers=HEADERS)).status_code == 413

    async def chunks():
        yield b"x" * 40
        yield b"x" * 40

    assert (await http.post(PATH, content=chunks(), headers=HEADERS)).status_code == 413
    application.state.write_limits.manual = RequestBudget(0)
    limited = await http.post(PATH, content=activity(), headers=HEADERS)
    assert limited.status_code == 429 and int(limited.headers["retry-after"]) > 0
    async with sessions() as session:
        assert await count(session, RawHealthData) == 0


async def test_existing_source_chain_controls_fit_duplicates_and_undo(client, sessions):
    from copy import deepcopy

    from app.parsers.identity import source_fingerprint
    from app.services.archive.storage import store_records
    from app.services.identities.identity_decisions import decide_sources
    from app.services.identities.identity_options import SourceDecision

    parsed = parse_fit(activity())
    current = parsed.records[FIT_EXERCISE][0]
    earlier = deepcopy(current)
    earlier["dataSource"]["device"]["displayName"] = "Synthetic earlier FIT label"
    first = source_fingerprint(earlier["dataSource"])
    second = source_fingerprint(current["dataSource"])
    async with sessions.begin() as session:
        await store_records(session, FIT_EXERCISE, [earlier], source="garmin-fit")
    http, _ = client
    assert (await http.post(PATH, content=activity(), headers=HEADERS)).status_code == 200
    async with sessions() as session:
        assert len(await workouts(session, START, START + timedelta(hours=1))) == 2
    await decide_sources(sessions, FIT_EXERCISE.name, request=SourceDecision("link", first, second))
    async with sessions() as session:
        assert len(await workouts(session, START, START + timedelta(hours=1))) == 1
        assert await count(session, ExerciseSession) == 2  # neither original was removed
    await decide_sources(
        sessions, FIT_EXERCISE.name, request=SourceDecision("reject", first, second)
    )
    async with sessions() as session:
        assert len(await workouts(session, START, START + timedelta(hours=1))) == 2


async def test_google_deletion_reconciliation_never_marks_fit_rows(client, sessions):
    from datetime import UTC, datetime

    from app.google_health.data_types import REGISTRY
    from app.services.archive.source_deletions import mark_source_deletions

    http, _ = client
    await http.post(PATH, content=activity(), headers=HEADERS)
    async with sessions.begin() as session:
        for kind in ("exercise", "heart-rate"):
            assert (
                await mark_source_deletions(
                    session,
                    REGISTRY[kind],
                    START,
                    START + timedelta(hours=1),
                    datetime.now(UTC) + timedelta(days=1),
                )
                == 0
            )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(RawHealthData)
                .where(RawHealthData.source_deleted_at.is_not(None))
            )
            == 0
        )
