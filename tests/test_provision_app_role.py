"""Input rules of scripts/provision_app_role.py; the database part is in tests/integration."""

from __future__ import annotations

import pytest

from scripts import provision_app_role as provisioner


@pytest.fixture(autouse=True)
def no_connection(monkeypatch):
    def forbidden(*args, **kwargs):  # pragma: no cover
        raise AssertionError("must not connect")

    monkeypatch.setattr(provisioner.psycopg, "connect", forbidden)


def test_explicit_owner_runtime_opt_out_is_left_alone(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://health:long-owner-password@db/health")
    monkeypatch.delenv("POSTGRES_APP_USER", raising=False)
    monkeypatch.delenv("POSTGRES_APP_PASSWORD", raising=False)
    monkeypatch.setenv("POSTGRES_ALLOW_OWNER_RUNTIME", "1")
    assert provisioner.main() == 0
    assert "nothing to do" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("user", "password", "message"),
    [
        ("healthos_app", "", "together"),
        ("", "a" * 32, "together"),
        ("Bad-Name", "a" * 32, "identifier"),
        ("health", "a" * 32, "owner"),
        ("postgres", "a" * 32, "owner"),
        ("healthos_app", "short", "URL-safe"),
        ("healthos_app", "a" * 31 + " ", "URL-safe"),
        ("healthos_app", "p@ss/word" * 4, "URL-safe"),
    ],
)
def test_bad_configuration_is_refused_before_connecting(
    monkeypatch, capsys, user, password, message
):
    monkeypatch.setenv("DATABASE_URL", "postgresql://health:long-owner-password@db/health")
    monkeypatch.setenv("POSTGRES_APP_USER", user)
    monkeypatch.setenv("POSTGRES_APP_PASSWORD", password)
    assert provisioner.main() == 2
    assert message in capsys.readouterr().err
