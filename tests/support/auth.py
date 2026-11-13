"""Login test helpers: a token and an application built with it."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from app import main
from app.core.config import Settings

TOKEN = "t" * 20 + "-test-ui-token-0123456789"


BASE = "http://localhost:8000"


def build(monkeypatch, token=TOKEN, **overrides):
    settings = Settings(
        _env_file=None,
        app_env="test",
        app_ui_token=token,
        codex_bridge_token=None,
        google_health_webhook_endpoint_uri="https://hooks.example/webhooks/google-health",
        google_health_webhook_authorization="Bearer webhook-secret-0123456789",
        **overrides,
    )
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(
        main,
        "build_runtime",
        lambda *_: SimpleNamespace(webhook_status=SimpleNamespace(record=AsyncMock())),
    )
    application = main.create_app()

    @application.get("/api/probe")
    async def api_probe():
        return {"reached": True}

    @application.get("/page-probe")
    async def page_probe():
        return {"reached": True}

    return application
