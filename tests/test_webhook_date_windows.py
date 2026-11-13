"""Synthetic signed deliveries and archived retries at representable date limits."""

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest
from sqlalchemy import select
from support.webhooks import bounded, notification
from support.webhooks import fetching_receiver as fetching_receiver
from support.webhooks import receiver as receiver

from app.google_health.data_types import REGISTRY, DataTypeSpec, QueryWindowError, query_iso
from app.models.retrieval import RetrievalReceipt
from app.models.state import WebhookBatchItem, WebhookEvent, WebhookNotification
from app.parsers.common import payload_hash
from app.services.synchronization import webhook_metadata
from app.services.synchronization.webhook_metadata import (
    NotificationWindowError,
    notification_windows,
)


def enable(r):
    r.incremental.specs = dict(REGISTRY)
    # The daily error must be detected even when it occurs in a later piece.
    kind = "daily-resting-heart-rate"
    r.incremental.specs[kind] = replace(REGISTRY[kind], max_window_days=1)
    r.processor.supported_data_types = set(r.incremental.specs)


@pytest.mark.parametrize("state", ["fresh", "legacy", "body-only", "pending", "failed"])
@pytest.mark.parametrize(
    "kind,intervals,error",
    [
        (
            kind,
            bounded(kind, start, end)["data"]["intervals"],
            "notification query window exceeds the representable date range",
        )
        for kind, start, end in [
            ("exercise", "0001-01-01T00:00:00Z", "0001-01-01T01:00:00Z"),
            ("exercise", "9999-12-30T23:00:00Z", "9999-12-31T12:00:00Z"),
            ("sleep", "9999-12-31T22:00:00Z", "9999-12-31T23:59:59Z"),
            ("weight", "9999-12-31T23:59:59Z", "9999-12-31T23:59:59.000001Z"),
            ("daily-resting-heart-rate", "9999-12-28T12:00:00Z", "9999-12-31T12:00:00Z"),
            ("calories-in-heart-rate-zone", "9999-12-01T00:00:00Z", "9999-12-31T23:59:59Z"),
        ]
    ]
    + [
        (
            "calories-in-heart-rate-zone",
            bounded("calories-in-heart-rate-zone", start, end)["data"]["intervals"],
            "notification query window budget exceeded",
        )
        for start, end in [
            ("0002-01-01T00:00:00Z", "9998-01-01T00:00:00Z"),
            *(
                ("2020-01-01T00:00:00Z", (datetime(2020, 1, 1, tzinfo=UTC) + delta).isoformat())
                for delta in [timedelta(days=14 * 257), timedelta(days=14 * 256, microseconds=1)]
            ),
        ]
    ]
    + [
        (
            "steps",
            intervals,
            "notification physicalTimeInterval must have valid startTime before endTime",
        )
        for physical in [
            None,
            [],
            {},
            *({bound: "2026-08-17T10:00:00Z"} for bound in ("startTime", "endTime")),
            *(
                {bound: value, other: "2026-08-17T10:00:00Z"}
                for bound, other in [("startTime", "endTime"), ("endTime", "startTime")]
                for value in [
                    None,
                    42,
                    "private-invalid-date",
                    "0001-01-01T00:00:00+00:01",
                    "9999-12-31T23:59:59-00:01",
                ]
            ),
            {"startTime": "2026-08-17T10:00:00Z", "endTime": "2026-08-17T10:00:00Z"},
            {"startTime": "2026-08-17T10:01:00Z", "endTime": "2026-08-17T10:00:00Z"},
        ]
        for intervals in [
            [{"physicalTimeInterval": physical}],
            notification()["data"]["intervals"] + [{"physicalTimeInterval": physical}],
            [{"physicalTimeInterval": physical}] + notification()["data"]["intervals"],
        ]
    ],
)
async def test_invalid_windows_are_terminal_rejections(
    fetching_receiver, sessions, state, kind, intervals, error
):
    r = fetching_receiver
    enable(r)
    good, bad = notification(), notification(kind)
    bad["data"]["intervals"] = intervals
    payload = [bad, good]
    raw = json.dumps(payload, indent=2).encode() + b"\n"
    digest = hashlib.sha256(raw).hexdigest()
    prepared = state in {"pending", "failed"}
    if state == "fresh":
        assert (await r.post(raw)).status_code == 204
        await r.finish()
    else:
        async with sessions.begin() as session:
            session.add(
                WebhookEvent(
                    payload_hash=digest,
                    raw_body=raw,
                    raw_json=None if state == "body-only" else payload,
                    signature_verified_at=r.current.now,
                )
            )
            if prepared:
                for ordinal, item in enumerate(payload, 1):
                    data = item["data"]
                    session.add(
                        WebhookNotification(
                            element_hash=payload_hash(item),
                            data_type=data["dataType"],
                            health_user_id=data["healthUserId"],
                            operation=data["operation"],
                            intervals=data["intervals"],
                            raw_json=item,
                            is_valid=True,
                            status="completed" if ordinal == 2 else state,
                            attempt_count=3,
                            sync_attempt_count=2,
                            sync_success_count=1,
                            last_error=None if ordinal == 2 else "old processing failure",
                        )
                    )
                await session.flush()
                session.add_all(
                    WebhookBatchItem(batch_hash=digest, ordinal=n, element_hash=payload_hash(item))
                    for n, item in enumerate(payload, 1)
                )
        result = await r.processor.process_pending(recover_running=False)
        assert (result.completed_elements, result.rejected_elements, result.failed_elements) == (
            1,
            1,
            0,
        )

    async with sessions() as session:
        batch = await session.get(WebhookEvent, digest)
        rejected = await session.get(WebhookNotification, payload_hash(bad))
        completed = await session.get(WebhookNotification, payload_hash(good))
        assert batch.raw_body == raw
        assert batch.raw_json == (None if state == "body-only" else payload)
        assert batch.signature_verified_at.replace(tzinfo=UTC) == r.current.now
        assert batch.processed_at is not None and batch.failure_count == 1
        assert rejected.status == "rejected" and not rejected.is_valid
        assert rejected.validation_error == batch.last_error == error
        assert rejected.last_error is None
        assert rejected.raw_json == bad and rejected.intervals == bad["data"]["intervals"]
        assert (rejected.sync_attempt_count, rejected.sync_success_count) == (
            (2, 1) if prepared else (0, 0)
        )
        assert completed.status == "completed" and completed.raw_json == good
        assert (
            completed.attempt_count,
            completed.sync_attempt_count,
            completed.sync_success_count,
        ) == ((3, 2, 1) if prepared else (1, 1, 1))
        attempts = (rejected.attempt_count, completed.attempt_count)
        members = (await session.scalars(select(WebhookBatchItem))).all()
        assert sorted((m.ordinal, m.element_hash) for m in members) == [
            (n, payload_hash(item)) for n, item in enumerate(payload, 1)
        ]

    # Both explicit replay and redelivery preserve terminal decisions and never
    # fetch the completed sibling again, including siblings completed before upgrade.
    result = await r.processor.process_batch(digest)
    assert (result.rejected_elements, result.failed_elements) == (1, 0)
    assert (await r.processor.process_pending(recover_running=False)).batches == 0
    assert (await r.post(raw)).status_code == 204
    await r.finish()
    assert [call[0] for call in r.incremental.calls] == ([] if prepared else ["steps"])
    async with sessions() as session:
        bad_row = await session.get(WebhookNotification, payload_hash(bad))
        good_row = await session.get(WebhookNotification, payload_hash(good))
        assert (bad_row.attempt_count, good_row.attempt_count) == attempts
        batch = await session.get(WebhookEvent, digest)
        assert batch.raw_body == raw
        assert batch.raw_json == (None if state == "body-only" else payload)
        assert bad_row.raw_json == bad and good_row.raw_json == good
        receipts = (await session.scalars(select(RetrievalReceipt))).all()
        assert (len(receipts), bad_row.status) == (0 if prepared else 1, "rejected")
    counters = (await r.status.snapshot())["counters"]["reasons"]
    assert counters["notification_rejected"] == 1
    assert counters.get("processing_failed", 0) == counters.get("batch_processing_failed", 0) == 0


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize(
    "kind,start,end,expected_start,expected_end,pieces",
    [
        (
            "exercise",
            "0001-01-01T12:00:00Z",
            "0001-01-01T13:00:00Z",
            "0001-01-01T00:00:00Z",
            "0001-01-02T03:00:00Z",
            1,
        ),
        (
            "exercise",
            "9999-12-31T08:00:00Z",
            "9999-12-31T09:59:59Z",
            "9999-12-30T20:00:00Z",
            "9999-12-31T23:59:59Z",
            1,
        ),
        (
            "sleep",
            "9999-12-31T22:00:00Z",
            "9999-12-31T23:59:58Z",
            "9999-12-31T22:00:00Z",
            "9999-12-31T23:59:59Z",
            1,
        ),
        (
            "daily-resting-heart-rate",
            "9999-12-28T12:00:00Z",
            "9999-12-31T00:00:00Z",
            "9999-12-28T12:00:00Z",
            "9999-12-31T00:00:00Z",
            3,
        ),
        (
            "calories-in-heart-rate-zone",
            "9999-12-01T00:00:00Z",
            "9999-12-31T23:59:00Z",
            "9999-12-01T00:00:00Z",
            "9999-12-31T23:59:00Z",
            3,
        ),
    ],
)
async def test_representable_windows_keep_complete_coverage(
    fetching_receiver, sessions, legacy, kind, start, end, expected_start, expected_end, pieces
):
    r = fetching_receiver
    enable(r)
    payload = bounded(kind, start, end)
    raw = json.dumps(payload).encode()
    if legacy:
        async with sessions.begin() as session:
            session.add(
                WebhookEvent(
                    payload_hash=hashlib.sha256(raw).hexdigest(), raw_body=raw, raw_json=payload
                )
            )
        result = await r.processor.process_pending(recover_running=False)
        assert (result.completed_elements, result.failed_elements) == (1, 0)
    else:
        assert (await r.post(raw)).status_code == 204
        await r.finish()
    calls = r.incremental.calls
    assert len(calls) == pieces
    assert calls[0][1] == datetime.fromisoformat(expected_start)
    assert calls[-1][2] == datetime.fromisoformat(expected_end)
    for index, (data_type, left, right) in enumerate(calls):
        assert data_type == kind and left < right
        if index:
            assert left == calls[index - 1][2]
        spec = r.incremental.specs[kind]
        if spec.max_window_days:
            assert right - left <= timedelta(days=spec.max_window_days)
            if index < pieces - 1:
                assert right - left == timedelta(days=spec.max_window_days)
        if kind.startswith("daily-"):
            upper_date = "9999-12-30" if index == 0 else "9999-12-31"
            assert spec.filter_for(query_iso(left), query_iso(right)).endswith(f'"{upper_date}"')
    async with sessions() as session:
        row = await session.scalar(select(WebhookNotification))
        assert row.status == "completed" and row.is_valid
        assert row.sync_attempt_count == row.sync_success_count == pieces
    await r.processor.process_pending(recover_running=False)
    assert len(r.incremental.calls) == pieces


def test_daily_filter_rejects_unrepresentable_exclusive_date():
    with pytest.raises(QueryWindowError, match="daily filter upper date"):
        REGISTRY["daily-resting-heart-rate"].filter_for(
            "9999-12-31T00:00:00Z", "9999-12-31T23:59:59Z"
        )


@pytest.mark.parametrize("intervals", [[], [{}], [None, 42, {}], [{"otherInterval": {}}]])
async def test_absent_physical_intervals_keep_default_window(receiver, intervals):
    item = notification()
    item["data"]["intervals"] = intervals
    assert (await receiver.post(json.dumps(item).encode())).status_code == 204
    await receiver.finish()
    assert receiver.incremental.calls == [("steps", None, None)]


def test_valid_physical_intervals_keep_covering_range():
    intervals = [notification(minute=m)["data"]["intervals"][0] for m in (3, 0)] + [{}]
    assert notification_windows(REGISTRY["steps"], intervals) == [
        (datetime(2026, 8, 17, 10, tzinfo=UTC), datetime(2026, 8, 17, 10, 4, tzinfo=UTC))
    ]


@pytest.mark.parametrize("last_piece", [timedelta(days=14), timedelta(microseconds=1)])
def test_window_budget_accepts_256_complete_or_partial_pieces(last_piece):
    spec = REGISTRY["calories-in-heart-rate-zone"]
    start = datetime(2020, 1, 1, tzinfo=UTC)
    end = start + timedelta(days=14 * 255) + last_piece
    windows = notification_windows(
        spec, bounded(spec.name, start.isoformat(), end.isoformat())["data"]["intervals"]
    )
    assert len(windows) == 256
    assert windows[0][0] == start and windows[-1][1] == end
    assert all(left[1] == right[0] for left, right in zip(windows, windows[1:], strict=False))


@pytest.mark.parametrize("kind", ["calories-in-heart-rate-zone", "weight", "exercise", "sleep"])
def test_budget_rejection_precedes_every_piece_validation(monkeypatch, kind):
    spec = REGISTRY[kind]
    if kind != "calories-in-heart-rate-zone":
        spec = replace(spec, max_window_days=14)
    start = datetime(2020, 1, 1, tzinfo=UTC)
    # Sessions cross the budget only after covering_window widens 256 pieces.
    end = start + timedelta(days=14 * 256)
    if kind in {"calories-in-heart-rate-zone", "weight"}:
        start, end = datetime(2, 1, 1, tzinfo=UTC), datetime(9998, 1, 1, tzinfo=UTC)
    validate = Mock(side_effect=AssertionError("must reject before iterating"))
    monkeypatch.setattr(webhook_metadata, "minute_aligned_range", validate)
    monkeypatch.setattr(DataTypeSpec, "filter_for", validate)
    with pytest.raises(
        NotificationWindowError, match="^notification query window budget exceeded$"
    ):
        notification_windows(
            spec, bounded(kind, start.isoformat(), end.isoformat())["data"]["intervals"]
        )
    assert validate.call_count == 0
