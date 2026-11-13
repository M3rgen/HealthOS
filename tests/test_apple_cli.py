"""healthos apple-token, apple-import, apple-status and apple-normalize (synthetic)."""

from __future__ import annotations

import asyncio
import json
import stat

import pytest
from apple_fixtures import export_zip, full_export
from cli_records import cli_database as cli_database
from sqlalchemy import func, select
from typer.testing import CliRunner

from app.cli import main as cli
from app.core.config import Settings
from app.models.apple import AppleImport, AppleRawRecord

RUNNER = CliRunner()


@pytest.fixture
def settings(monkeypatch, tmp_path):
    value = Settings(
        _env_file=None,
        app_env="test",
        codex_bridge_token=None,
        apple_receiver_token_file=tmp_path / "secrets" / "apple-token",
    )
    monkeypatch.setattr(cli, "get_settings", lambda: value)
    return value


def mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_token_is_created_private_and_printed_once(settings):
    result = RUNNER.invoke(cli.app, ["apple-token"])
    assert result.exit_code == 0, result.output
    target = settings.apple_receiver_token_file
    token = target.read_text().strip()
    assert len(token) >= 43
    assert mode(target) == 0o600
    assert mode(target.parent) == 0o700
    assert f"Authorization: Bearer {token}" in result.stdout
    assert result.stdout.count(token) == 1
    # No temporary file is left next to it.
    assert [item.name for item in target.parent.iterdir()] == [target.name]


def test_existing_token_is_kept_without_rotate(settings):
    assert RUNNER.invoke(cli.app, ["apple-token"]).exit_code == 0
    target = settings.apple_receiver_token_file
    before = target.read_text()
    refused = RUNNER.invoke(cli.app, ["apple-token"])
    assert refused.exit_code == 1
    assert "--rotate" in refused.stderr
    assert target.read_text() == before
    rotated = RUNNER.invoke(cli.app, ["apple-token", "--rotate"])
    assert rotated.exit_code == 0
    after = target.read_text()
    assert after != before
    assert mode(target) == 0o600
    assert after.strip() in rotated.stdout


def test_token_file_option_overrides_the_setting(settings, tmp_path):
    target = tmp_path / "elsewhere" / "token"
    result = RUNNER.invoke(cli.app, ["apple-token", "--file", str(target)])
    assert result.exit_code == 0, result.output
    assert mode(target) == 0o600
    assert not settings.apple_receiver_token_file.exists()


async def _counts(sessions):
    async with sessions() as session:
        records = await session.scalar(select(func.count()).select_from(AppleRawRecord))
        imports = (await session.scalars(select(AppleImport))).all()
    return records, imports


def test_import_of_a_zip_and_again(settings, cli_database, tmp_path):
    path = tmp_path / "export.zip"
    path.write_bytes(export_zip(full_export()))
    first = RUNNER.invoke(cli.app, ["apple-import", str(path)])
    assert first.exit_code == 0, first.output
    result = json.loads(first.stdout)
    assert result["status"] == "done" and result["origin"] == "cli"
    assert result["filename"] == "export.zip"
    assert result["summary"]["format"] == "zip"
    assert result["records_new"] == result["records_read"] > 5
    again = RUNNER.invoke(cli.app, ["apple-import", str(path)])
    assert again.exit_code == 0, again.output
    assert json.loads(again.stdout)["records_new"] == 0
    records, imports = asyncio.run(_counts(cli_database))
    assert records == result["records_new"]
    assert [item.status for item in imports] == ["done", "done"]

    status = RUNNER.invoke(cli.app, ["apple-status"])
    assert status.exit_code == 0, status.output
    report = json.loads(status.stdout)
    assert report["receiver"] == {
        "enabled": False,
        "token_file_present": False,
        "endpoint_configured": False,
    }
    assert len(report["imports"]) == 2

    normalized = RUNNER.invoke(cli.app, ["apple-normalize"])
    assert normalized.exit_code == 0, normalized.output
    stats = json.loads(normalized.stdout)
    # Everything was normalized during the import already.
    assert stats["inserted"] == 0 and stats["updated"] == 0
    since = RUNNER.invoke(cli.app, ["apple-normalize", "--since", "2026-11-01"])
    assert since.exit_code == 0, since.output
    bad = RUNNER.invoke(cli.app, ["apple-normalize", "--since", "yesterday"])
    assert bad.exit_code == 2


def test_import_of_a_bare_xml(settings, cli_database, tmp_path):
    path = tmp_path / "export.xml"
    path.write_bytes(full_export())
    result = RUNNER.invoke(cli.app, ["apple-import", str(path)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["summary"]["format"] == "xml"


def test_import_refuses_what_is_not_an_export(settings, cli_database, tmp_path):
    missing = RUNNER.invoke(cli.app, ["apple-import", str(tmp_path / "nothing.zip")])
    assert missing.exit_code == 2
    garbage = tmp_path / "notes.xml"
    garbage.write_text("<notes><unclosed></notes>")
    result = RUNNER.invoke(cli.app, ["apple-import", str(garbage)])
    assert result.exit_code == 1
    assert "okunamadı" in result.stderr
    _, imports = asyncio.run(_counts(cli_database))
    assert [item.status for item in imports] == ["failed"]
