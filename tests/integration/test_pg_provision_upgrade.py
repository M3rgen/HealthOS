"""provision-app-role tells an empty earlier install from a new one, on real PostgreSQL."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.models.webhook_status import WebhookStatusEvent
from scripts import provision_app_role as provisioner

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_a_database_an_application_already_ran_on_is_not_a_new_install(
    pg_sessions, migrated, database_url, monkeypatch, capsys
):
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.delenv("POSTGRES_APP_USER", raising=False)
    monkeypatch.delenv("POSTGRES_APP_PASSWORD", raising=False)
    monkeypatch.delenv("POSTGRES_ALLOW_OWNER_RUNTIME", raising=False)
    # The guard still holds on a database no application has started on.
    assert provisioner.main() == 2
    assert "new install" in capsys.readouterr().err
    async with pg_sessions() as session:
        session.add(WebhookStatusEvent(reason="monitoring_started", occurred_at=datetime.now(UTC)))
        await session.commit()
    assert provisioner.main() == 0
    assert "existing install" in capsys.readouterr().out
