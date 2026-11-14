"""Bounded synthetic heart archives with publication and pre-membership histories."""

from datetime import UTC, datetime, timedelta
from random import Random

from sqlalchemy import insert

from app.models.garmin import (
    GarminApiResponse,
    GarminHeartMember,
    GarminHeartSet,
    GarminRawRecord,
    GarminRecordObservation,
)
from app.models.health import HeartRate
from app.services.providers.garmin_normalize import build_rows

START = datetime(2000, 1, 1, tzinfo=UTC)
STOP = datetime(2020, 1, 1, tzinfo=UTC)
SEEN = datetime(2026, 1, 1, tzinfo=UTC)


def normalized_versions(normalized, case):
    for row in normalized:
        row["is_quarantined"] = case == 6
        if case in (7, 8):
            yield {**row, "google_data_point_id": row["google_data_point_id"].removesuffix(":v2")}
        if case == 9:
            row["source_record_id"] = row["source_record_id"].upper()
        if case == 10:
            row["source_record_id"] = None
        if case != 7:
            yield row


async def grow_archive(sessions, first, last, *, all_legacy=False, seed=739):
    """Three samples/source; stale normalized values expose the selected raw signs.

    Mixed archives include completed A + pending B, B -> A, empty completion, max-id
    legacy, equal-time sightings returning to A, normalized-only, quarantine, v1,
    v1/v2, a foreign source prefix, no source, and completed negative B.
    """
    rng = Random(seed)
    for offset in range(first, last, 250):
        raw, rows, sightings, sets, members = [], [], [], [], []
        for i in range(offset, min(last, offset + 250)):
            day = (START + timedelta(days=i)).date()
            # Calendar keys intentionally precede their samples by a day.
            stamp = int((START + timedelta(days=i + 1)).timestamp() * 1000)
            case = 3 + i % 2 if all_legacy else i % 12
            source = f"heart-rate:{day}"
            body = {"heartRateValues": [[stamp + j * 60000, 60 + j] for j in range(3)]}
            built, _ = build_rows({"heart-rate": {str(day): body}}, day, day, "Synthetic")
            normalized = built[HeartRate]
            rows.extend(normalized_versions(normalized, case))
            if case == 5:
                continue
            ids = [3 * i + 1, 3 * i + 2]
            for version, identifier in enumerate(ids):
                value = rng.choice((60, "-1e-1000", "-0e-1000", 0)) if version == 0 else "-1e-1000"
                samples = [[stamp, 10**30], [stamp, value], [stamp, 80]] + body["heartRateValues"][
                    1:
                ]
                raw.append(
                    dict(
                        id=identifier,
                        kind="heart-rate",
                        key=str(day),
                        body={"heartRateValues": [] if case == 2 and version else samples},
                        body_sha256=f"{identifier:064x}",
                        first_seen_at=SEEN,
                    )
                )
            # Unrelated kind sharing the same key must never participate.
            raw.append({**raw[-1], "id": 3 * i + 3, "kind": "steps"})
            if case == 4:
                sightings.extend(
                    dict(raw_record_id=identifier, response_id=response, observed_at=SEEN)
                    for response, identifier in enumerate([*ids, ids[0]], 1)
                )
            if case not in (0, 1, 2, 11):
                continue
            for n, identifier in enumerate([ids[1], ids[0]] if case == 1 else [ids[case != 0]]):
                set_id = 2 * i + n + 1
                sets.append(
                    dict(
                        id=set_id,
                        source_record_id=source,
                        raw_record_id=identifier,
                        members_sha256="0" * 64,
                    )
                )
                if case != 2:
                    members.extend(
                        dict(set_id=set_id, sample_key=r["google_data_point_id"])
                        for r in normalized
                    )
        async with sessions.begin() as session:
            if first == offset == 0:
                session.add_all(
                    GarminApiResponse(
                        id=i,
                        fetched_at=SEEN,
                        kind="heart-rate",
                        method="GET",
                        path="/synthetic",
                        request_sha256="0" * 64,
                        status_code=200,
                        body=b"[]",
                        body_sha256="0" * 64,
                        body_size=2,
                    )
                    for i in (1, 2, 3)
                )
                await session.flush()
            for model, values in (
                (GarminRawRecord, raw),
                (HeartRate, rows),
                (GarminRecordObservation, sightings),
                (GarminHeartSet, sets),
                (GarminHeartMember, members),
            ):
                if values:
                    await session.execute(insert(model), values)
