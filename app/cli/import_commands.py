"""Local FIT files enter the same archive transaction as binary API uploads."""

import json
from dataclasses import asdict
from pathlib import Path

import typer

from app.cli._context import context
from app.core.fit import MAX_FIT_BYTES
from app.services.providers.fit_decode import FitError
from app.services.providers.fit_import import import_fit_bytes

PATH_ARGUMENT = typer.Argument(..., exists=True, dir_okay=False, readable=True)


def import_fit(path: Path = PATH_ARGUMENT) -> None:
    """Import a FIT activity file (Garmin or other devices) into the archive."""
    with path.open("rb") as file:
        data = file.read(MAX_FIT_BYTES + 1)
    runtime = context.runtime()

    async def command():
        try:
            async with context.sessions() as session:
                result = await import_fit_bytes(session, data)
                await session.commit()
                return result
        finally:
            await runtime.shutdown()

    try:
        result = context.run(command())
    except FitError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from None
    typer.echo(json.dumps(asdict(result)))
