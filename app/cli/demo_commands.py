"""Synthetic sample data for trying HealthOS without a Google or Garmin account."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import typer

from app.cli._context import context


def demo(
    remove: bool = typer.Option(
        False, "--remove", help="Delete the sample data again, and nothing else."
    ),
    timezone: str = typer.Option(
        "UTC", "--timezone", help="IANA time zone whose calendar days the sample fills."
    ),
) -> None:
    """Load synthetic sample data into an empty archive.

    About 90 days of an invented person (steps, heart rate, sleep, weight, workouts), labelled
    as sample data everywhere it is shown; --remove deletes it again and nothing else.
    """
    from app.services.providers import demo_archive as service

    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        typer.echo(f"Saat dilimi geçersiz: {timezone}", err=True)
        raise typer.Exit(2) from None

    async def command():
        async with context.sessions.begin() as session:
            if remove:
                result = {"removed": await service.remove_sample(session)}
            else:
                result = {"added": await service.load_sample(session, zone, datetime.now(UTC))}
            return result | await service.sample_status(session)

    try:
        summary = context.run(command())
    except service.SampleRefused as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None
    typer.echo(json.dumps(summary, indent=2, default=str, ensure_ascii=False))
    if not remove:
        typer.echo(
            "Örnek veri yüklendi: uydurma bir kişinin verisi, gerçek ölçüm değil. "
            "Kaldırmak için: healthos demo --remove",
            err=True,
        )
