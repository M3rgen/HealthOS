"""Apple Health: the receiver token, an export import from disk, status and re-normalizing."""

from __future__ import annotations

import json
import os
import secrets
import time
from datetime import UTC, date, datetime
from pathlib import Path

import typer

from app.cli._context import context

TOKEN_FILE_OPTION = typer.Option(
    None, "--file", help="Belirteç dosyası; varsayılan APPLE_RECEIVER_TOKEN_FILE."
)
PATH_ARGUMENT = typer.Argument(..., help="export.zip veya export.xml.")
# The CLI reads a file the owner chose on their own disk: the upload limit does not apply.
NO_LIMIT = 1 << 62


def apple_token(
    rotate: bool = typer.Option(False, "--rotate", help="Var olan belirteci yenisiyle değiştir."),
    file: Path | None = TOKEN_FILE_OPTION,
) -> None:
    """Health Auto Export alıcısının belirtecini oluştur (dosya 600) ve bir kez göster."""
    target = (file or context.settings().apple_receiver_token_file).expanduser()
    if target.exists() and not rotate:
        typer.echo(
            f"{target} zaten var; değiştirmek için --rotate ekle (eski belirteç geçersiz olur).",
            err=True,
        )
        raise typer.Exit(1)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(target.parent, 0o700)
    token = secrets.token_urlsafe(32)
    temporary = target.with_name(f".{target.name}.new")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(token + "\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    typer.echo(f"Belirteç {target} dosyasına yazıldı (yalnız sen okuyabilirsin).")
    typer.echo("Health Auto Export'ta REST API otomasyonuna şu başlığı ekle:")
    typer.echo(f"Authorization: Bearer {token}")
    typer.echo("Bu belirteç bir daha gösterilmez; kaybolursa --rotate ile yenisini oluştur.")


def apple_import(
    path: Path = PATH_ARGUMENT,
) -> None:
    """Sağlık uygulamasının dışa aktarmasını (export.zip/export.xml) arşive al."""
    if not path.is_file():
        typer.echo(f"Dosya bulunamadı: {path}", err=True)
        raise typer.Exit(2)
    from app.services.providers import apple_import as service

    kind = service.export_kind(path)
    sessions = context.sessions
    max_output = context.settings().apple_import_max_uncompressed
    last = [time.monotonic()]

    async def chunks():
        # Progress on stderr every few seconds; the summary goes to stdout as JSON.
        for chunk in service.export_chunks(path, max_output):
            yield chunk
            current = service.running()
            now = time.monotonic()
            if current is not None and now - last[0] >= 5:
                last[0] = now
                typer.echo(
                    f"{current['xml_bytes'] / 1048576:,.0f} MB okundu · "
                    f"{current['records_read']:,} kayıt · {current['records_new']:,} yeni",
                    err=True,
                )

    async def command():
        return await service.import_stream(
            sessions,
            chunks(),
            origin=service.CLI,
            filename=path.name,
            max_bytes=NO_LIMIT,
            reader=service.XmlReader(kind),
        )

    try:
        result = context.run(command())
    except service.AppleImportError as exc:
        typer.echo(exc.message, err=True)
        raise typer.Exit(1) from None
    typer.echo(json.dumps(result, indent=2, default=str, ensure_ascii=False))


def apple_status() -> None:
    """Apple Sağlık alıcısının ayarını ve son içe aktarmaları göster (değer ve sır içermez)."""
    from app.core.apple_paths import receiver_token
    from app.services.providers import apple_import as service

    settings = context.settings()
    sessions = context.sessions

    async def command():
        async with sessions() as session:
            return await service.recent_imports(session)

    report = {
        "receiver": {
            "enabled": settings.apple_receiver_enabled,
            "token_file_present": receiver_token(settings) is not None,
            "endpoint_configured": settings.apple_receiver_endpoint_uri is not None,
        },
        "imports": context.run(command()),
    }
    typer.echo(json.dumps(report, indent=2, default=str, ensure_ascii=False))


def apple_normalize(
    since: str | None = typer.Option(None, "--since", help="İlk gün, YYYY-AA-GG."),
) -> None:
    """Arşivdeki Apple Sağlık kayıtlarını normalize tablolara yeniden işle."""
    try:
        start = date.fromisoformat(since) if since else None
    except ValueError as exc:
        typer.echo(f"Tarih biçimi YYYY-AA-GG olmalı: {exc}", err=True)
        raise typer.Exit(2) from None
    from app.services.providers.apple_normalize import normalize_archive

    sessions = context.sessions

    async def command():
        async with sessions() as session:
            stats = await normalize_archive(
                session, since=datetime.combine(start, datetime.min.time(), UTC) if start else None
            )
            await session.commit()
            return stats

    stats = context.run(command())
    payload = stats.as_dict() if hasattr(stats, "as_dict") else stats
    typer.echo(json.dumps(payload, indent=2, default=str, ensure_ascii=False))
