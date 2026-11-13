"""POSTGRES_APP_PASSWORD meets the rule every other production secret meets."""

from __future__ import annotations

import secrets

import pytest

from scripts import provision_app_role as provisioner

LEGACY = "q7Zr-Lm2x-Vb9k-Tn4w-8cRp3"  # 25 URL-safe characters, passes only the earlier rule


@pytest.fixture(autouse=True)
def no_connection(monkeypatch):
    def forbidden(*args, **kwargs):  # pragma: no cover
        raise AssertionError("must not connect")

    monkeypatch.setattr(provisioner.psycopg, "connect", forbidden)


@pytest.mark.parametrize(
    "password", ["a" * 32, "abcdefghijklmnopqrstuvwxyz012345", "password" * 4, LEGACY]
)
def test_weak_app_password_is_refused_before_connecting(monkeypatch, capsys, password):
    monkeypatch.setenv("DATABASE_URL", "postgresql://health:owner@db/health")
    monkeypatch.setenv("POSTGRES_APP_USER", "healthos_app")
    monkeypatch.setenv("POSTGRES_APP_PASSWORD", password)
    monkeypatch.delenv("HEALTHOS_ALLOW_LEGACY_SECRETS", raising=False)
    assert provisioner.main() == 2
    error = capsys.readouterr().err
    assert "weaker than a generated secret" in error and password not in error


def test_generated_password_passes_the_rule():
    assert provisioner.requested_role("healthos_app", secrets.token_hex(32), owner="health")


def test_legacy_password_needs_the_explicit_upgrade_aid(capsys):
    with pytest.raises(provisioner.ProvisionError):
        provisioner.requested_role("healthos_app", LEGACY, owner="health")
    assert provisioner.requested_role("healthos_app", LEGACY, owner="health", allow_legacy=True)
    assert "HEALTHOS_ALLOW_LEGACY_SECRETS" in capsys.readouterr().err
    with pytest.raises(provisioner.ProvisionError):  # the aid never admits a weak one
        provisioner.requested_role("healthos_app", "a" * 32, owner="health", allow_legacy=True)
