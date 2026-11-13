"""Typed notification states, legacy rows and diagnostics independent of control flow."""

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import StatementError
from support.webhooks import Incremental, notification
from webhook_ownership_records import age, ingest, processor, snapshot

from app.core.config import Settings
from app.core.webhook_types import INTERRUPTED_ERROR, NotificationStatus, notification_status
from app.models.state import OAuthToken, WebhookEvent, WebhookNotification
from app.services.synchronization.pipeline_health import PipelineHealthService, PipelineStatus
from app.sync import webhook_processing, webhook_reads
from app.sync.incremental import STALE_RUNNING_AFTER


@pytest.mark.parametrize("offset,recovered", [(-1, 1), (0, 0), (1, 0)])
async def test_running_notification_cutoff_boundary(sessions, clock, offset, recovered):
    runner = processor(sessions, None)
    updated_at = clock.now - STALE_RUNNING_AFTER + timedelta(seconds=offset)
    async with sessions.begin() as session:
        session.add(
            WebhookNotification(
                element_hash="synthetic-boundary",
                raw_json={"synthetic": True},
                data_type="steps",
                is_valid=True,
                status=NotificationStatus.running,
                updated_at=updated_at,
            )
        )

    assert await runner.recover_running(clock.now) == recovered

    async with sessions() as session:
        row = await session.get(WebhookNotification, "synthetic-boundary")
        expected = NotificationStatus.interrupted if recovered else NotificationStatus.running
        assert row.status is expected
        assert row.last_error == (INTERRUPTED_ERROR if recovered else None)
        assert row.attempt_count == 0
        assert row.raw_json == {"synthetic": True}


@pytest.mark.parametrize("dialect", [postgresql.dialect(), sqlite.dialect()])
def test_status_keeps_the_existing_varchar_schema(dialect):
    column = WebhookNotification.__table__.c.status
    assert column.type.compile(dialect=dialect) == "VARCHAR(32)"
    assert not column.type.native_enum
    assert not column.type.create_constraint


@pytest.mark.parametrize("status", NotificationStatus)
async def test_plain_stored_states_load_as_the_same_enum(sessions, status):
    async with sessions.begin() as session:
        # Bypass ORM and bind validation, as a pre-existing VARCHAR row would.
        await session.execute(
            text(
                "INSERT INTO webhook_notifications (element_hash, raw_json, is_valid, status, "
                "attempt_count, sync_attempt_count, sync_success_count) "
                "VALUES ('synthetic', '{}', true, :status, 0, 0, 0)"
            ),
            {"status": status.value},
        )
    async with sessions() as session:
        row = await session.get(WebhookNotification, "synthetic")
        assert row.status is status
        assert row.raw_json == {}


def test_assignment_converts_known_strings_and_rejects_unknown_states():
    row = WebhookNotification(status="failed")
    assert row.status is NotificationStatus.failed
    with pytest.raises(ValueError, match="not a valid NotificationStatus"):
        row.status = "unknown"
    assert row.status is NotificationStatus.failed


async def test_unknown_core_write_is_rejected(sessions):
    async with sessions.begin() as session:
        with pytest.raises(StatementError, match="not among the defined enum values"):
            await session.execute(update(WebhookNotification).values(status="unknown"))


async def test_unknown_legacy_state_fails_processing_without_altering_the_archive(sessions):
    runner = processor(sessions, None)
    batch, digest = await ingest(runner)
    async with sessions.begin() as session:
        await session.execute(
            text("UPDATE webhook_notifications SET status = 'unknown' WHERE element_hash = :key"),
            {"key": digest},
        )
    async with sessions() as session:
        body = (await session.get(WebhookEvent, batch)).raw_body
        with pytest.raises(LookupError, match="not among the defined enum values"):
            await session.get(WebhookNotification, digest)
    with pytest.raises(LookupError, match="not among the defined enum values"):
        await runner.process_batch(batch)
    async with sessions() as session:
        assert (await session.get(WebhookEvent, batch)).raw_body == body
        assert await session.scalar(text("SELECT status FROM webhook_notifications")) == "unknown"
        assert await session.scalar(text("SELECT attempt_count FROM webhook_notifications")) == 0


@pytest.mark.parametrize(
    "status,error,expected",
    [
        (NotificationStatus.failed, INTERRUPTED_ERROR, NotificationStatus.interrupted),
        (NotificationStatus.failed, INTERRUPTED_ERROR + " extra", NotificationStatus.failed),
        (NotificationStatus.failed, None, NotificationStatus.failed),
        (NotificationStatus.running, INTERRUPTED_ERROR, NotificationStatus.running),
        (NotificationStatus.interrupted, "changed diagnostic", NotificationStatus.interrupted),
        (NotificationStatus.interrupted, None, NotificationStatus.interrupted),
    ],
)
def test_legacy_decoding_is_exact_and_new_states_ignore_diagnostics(status, error, expected):
    assert notification_status(status, error) is expected


async def test_sweep_retries_typed_and_legacy_interruptions_but_preserves_real_failures(
    sessions, monkeypatch
):
    runner = processor(sessions, None)
    runner.incremental = Incremental()
    async with sessions.begin() as session:
        session.add(
            OAuthToken(subject="me", encrypted_refresh_token="synthetic", health_user_id="user-1")
        )
    cases = [
        ("pending", None),
        ("failed", INTERRUPTED_ERROR),
        ("interrupted", "a different display message"),
        ("interrupted", None),
        ("failed", "HTTP failure"),
        ("failed", INTERRUPTED_ERROR + " extra"),
        ("failed", None),
        ("completed", None),
        ("running", None),
        ("rejected", None),
    ]
    digests = []
    for minute, (status, error) in enumerate(cases):
        payload = notification(minute=minute)
        result = await runner.ingest_verified(str(minute).encode(), payload)
        async with sessions.begin() as session:
            digest = await session.scalar(
                text("SELECT element_hash FROM webhook_batch_items WHERE batch_hash = :batch"),
                {"batch": result.batch_hash},
            )
            await session.execute(
                text(
                    "UPDATE webhook_notifications SET status = :status, last_error = :error "
                    "WHERE element_hash = :digest"
                ),
                {"digest": digest, "status": status, "error": error},
            )
            digests.append(digest)
    await age(sessions)
    # Cross multiple pages with equal timestamps; processing changes both status and timestamp.
    monkeypatch.setattr(webhook_reads, "READ_PAGE_SIZE", 2)
    before = await snapshot(sessions)
    result = await runner.process_unfinished()
    assert result.completed_elements == len(runner.incremental.calls) == 4
    after = await snapshot(sessions)
    assert before["webhook_batch_items"] == after["webhook_batch_items"]
    async with sessions() as session:
        for index, digest in enumerate(digests):
            row = await session.get(WebhookNotification, digest)
            if index < 4:
                assert row.status is NotificationStatus.completed
                assert row.attempt_count == 1
            else:
                assert row.status.value == cases[index][0]
                assert row.last_error == cases[index][1]
                assert row.attempt_count == 0
        for old in before["webhook_events"]:
            row = await session.get(WebhookEvent, old["payload_hash"])
            assert row.raw_json == old["raw_json"] and row.raw_body == old["raw_body"]
    assert (await runner.process_unfinished()).completed_elements == 0


async def test_new_recovery_remains_retryable_without_an_error_message(sessions):
    runner = processor(sessions, None)
    batch, digest = await ingest(runner)
    runner.incremental = Incremental()
    async with sessions.begin() as session:
        row = await session.get(WebhookNotification, digest)
        row.status = NotificationStatus.running
    await age(sessions)
    assert await runner.recover_running() == 1
    async with sessions.begin() as session:
        row = await session.get(WebhookNotification, digest)
        assert row.status is NotificationStatus.interrupted
        row.last_error = None
    await runner._finalize_batch(batch)
    async with sessions() as session:
        assert (await session.get(WebhookEvent, batch)).failure_count == 1
    health = await PipelineHealthService(Settings(_env_file=None), sessions, set()).inspect()
    assert health.failed_notifications == 1 and health.status is PipelineStatus.failed
    await age(sessions)
    assert (await runner.process_unfinished()).completed_elements == 1


@pytest.mark.parametrize("valid,age_minutes", [(True, 0), (False, 60)])
async def test_sweep_preserves_recent_or_invalid_notifications(sessions, valid, age_minutes):
    async with sessions.begin() as session:
        session.add(
            WebhookNotification(
                element_hash="excluded",
                raw_json={},
                status=NotificationStatus.interrupted,
                is_valid=valid,
                updated_at=datetime.now(UTC) - timedelta(minutes=age_minutes),
            )
        )
    assert [
        key
        async for key in webhook_reads.unfinished_hashes(
            sessions, datetime.now(UTC) - timedelta(minutes=5)
        )
    ] == []


def control_flow_violations(source):
    violations = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Compare):
            predicates = [node]
        elif isinstance(node, ast.If | ast.While | ast.IfExp):
            predicates = [node.test]
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"where", "filter", "in_", "not_in", "like", "ilike"}
        ):
            predicates = (
                node.args
                if node.func.attr in {"where", "filter"}
                else [node.func.value, *node.args]
            )
        else:
            continue
        for predicate in predicates:
            for part in ast.walk(predicate):
                if (
                    isinstance(part, ast.Attribute)
                    and part.attr == "last_error"
                    or isinstance(part, ast.Name)
                    and part.id == "last_error"
                    or isinstance(part, ast.Constant)
                    and isinstance(part.value, str)
                    and part.value in {s.value for s in NotificationStatus}
                ):
                    violations.append(node.lineno)
    return violations


def test_processing_control_flow_uses_states_instead_of_diagnostic_text():
    for module in (webhook_processing, webhook_reads):
        assert not control_flow_violations(Path(module.__file__).read_text())


@pytest.mark.parametrize(
    "source",
    [
        "row.last_error == INTERRUPTED_ERROR",
        "WebhookNotification.last_error == 'display text'",
        "result == 'completed'",
        "WebhookNotification.last_error.in_((INTERRUPTED_ERROR,))",
        "WebhookNotification.status.in_(('pending', 'failed'))",
        "if row.last_error:\n    retry()",
    ],
)
def test_control_flow_guard_rejects_text_discriminators(source):
    assert control_flow_violations(source)
