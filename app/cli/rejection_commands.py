"""Records set apart because PostgreSQL jsonb cannot hold them, and their acknowledgement."""

from __future__ import annotations

import json
import sys

import typer

from app.cli._context import context


def rejections(
    limit: int = typer.Option(20, min=1, max=10_000, help="Number of most recent records to list."),
    ack: int | None = typer.Option(
        None,
        "--ack",
        min=1,
        help="Acknowledge rejections through this ID (inclusive); clear their /health warning.",
    ),
    element: int | None = typer.Option(
        None, "--element", min=1, help="Write the original record with this ID to standard output."
    ),
) -> None:
    """List, acknowledge or export Google records rejected during synchronization."""
    from app.services.archive import raw_rejections as service

    if ack is not None and element is not None:
        typer.echo("--ack ve --element birlikte kullanılamaz.", err=True)
        raise typer.Exit(2)
    if element is not None:

        async def load():
            async with context.sessions() as session:
                return await service.load_element(session, element)

        data = context.run(load())
        if data is None:
            typer.echo(f"Reddedilen kayıt bulunamadı: {element}", err=True)
            raise typer.Exit(1)
        sys.stdout.buffer.write(data + b"\n")
        sys.stdout.buffer.flush()
        return
    if ack is not None:
        through = ack

        async def acknowledge():
            async with context.sessions.begin() as session:
                return await service.acknowledge(session, through)

        try:
            current, written = context.run(acknowledge())
        except service.UnknownRejection:
            typer.echo(f"Reddedilen kayıt bulunamadı: {ack}", err=True)
            raise typer.Exit(1) from None
        if written:
            typer.echo(
                f"Onaylandı: {current} numaralı reddedilen kayda kadar (dahil). Yeni bir kayıt "
                "reddedilene kadar /health bu nedenle uyarmaz."
            )
        else:
            typer.echo(f"Zaten onaylı: onay {current} numaralı kayda kadar; yeni onay yazılmadı.")
        return

    async def command():
        async with context.sessions() as session:
            return await service.list_rejections(session, limit=limit)

    typer.echo(json.dumps(context.run(command()), indent=2, ensure_ascii=False))
