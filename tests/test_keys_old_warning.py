"""Retired Google roots trigger one value-free warning without blocking startup."""

import logging
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet
from support.auth import build

from app import main


@pytest.mark.parametrize("populated", [True, False])
async def test_keys_old_warning_at_startup(monkeypatch, sessions, caplog, populated):
    roots = [Fernet.generate_key().decode() for _ in range(2)]
    monkeypatch.setattr(main, "configure_logging", lambda *_: None)
    application = build(
        monkeypatch,
        healthos_sources=["none"],
        google_token_encryption_keys_old=roots if populated else [],
    )
    monkeypatch.setattr(main, "SessionLocal", sessions)
    application.state.runtime.scheduler_needed = lambda _settings: False
    application.state.runtime.shutdown = AsyncMock()
    with caplog.at_level(logging.INFO, logger="app.main"):
        async with application.router.lifespan_context(application):
            assert any(getattr(r, "event", None) == "APPLICATION_READY" for r in caplog.records)
    warnings = [
        r for r in caplog.records if getattr(r, "event", None) == "GOOGLE_TOKEN_KEYS_OLD_IGNORED"
    ]
    assert len(warnings) == int(populated)
    if populated:
        assert warnings[0].count == 2
        assert warnings[0].levelno == logging.WARNING
    for root in roots:
        assert root not in caplog.text
        for record in caplog.records:
            assert all(root not in str(value) for value in record.__dict__.values())
    application.state.runtime.shutdown.assert_awaited_once()
