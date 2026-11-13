"""Engine creation belongs to startup; injected test/embedded database factories stay bound."""

from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import Settings
from app.db import session as database


@pytest.mark.parametrize("failure", [False, True])
async def test_startup_uses_current_settings_and_disposes_on_exit(monkeypatch, failure):
    engine = Mock(dispose=AsyncMock())
    create = Mock(return_value=engine)
    monkeypatch.setattr(database, "create_shared_engine", create)
    settings = Settings(_env_file=None, database_url="sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(expire_on_commit=False)
    assert factory.kw["bind"] is None
    if failure:
        with pytest.raises(RuntimeError, match="synthetic startup failure"):
            async with database.database_lifespan(settings, factory):
                assert factory.kw["bind"] is engine
                raise RuntimeError("synthetic startup failure")
    else:
        async with database.database_lifespan(settings, factory):
            assert factory.kw["bind"] is engine
            assert database.initialize_sessions(settings, factory) is None
    create.assert_called_once_with(settings)
    engine.dispose.assert_awaited_once()
    assert factory.kw["bind"] is None


async def test_external_factory_is_neither_rebound_nor_disposed(sessions, monkeypatch):
    create = Mock(side_effect=AssertionError("injected database must stay in use"))
    monkeypatch.setattr(database, "create_shared_engine", create)
    engine = sessions.kw["bind"]
    settings = Settings(_env_file=None)
    async with database.database_lifespan(settings, sessions):
        assert sessions.kw["bind"] is engine
    async with sessions() as session:
        assert await session.scalar(text("SELECT 1")) == 1
    create.assert_not_called()


async def test_health_tool_requires_an_injected_archive_factory(sessions):
    from app.services.umay.codex_health import read_health

    result = await read_health(sessions, {"action": "inventory"})
    assert result["streams"] == []
    with pytest.raises(RuntimeError, match="session factory"):
        await read_health(None, {"action": "inventory"})
