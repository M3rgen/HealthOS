"""Garmin Connect Personal API: status, one incremental pull, and the historical backfill."""

from __future__ import annotations

import json
from datetime import date

import typer

from app.cli._context import context
from app.runtime import Runtime
from app.services.providers.garmin_pull import GarminPull

KINDS_OPTION = typer.Option(
    None,
    "--kind",
    help="Pull only these kinds (repeatable); default: all kinds.",
)


def _garmin(runtime: Runtime) -> GarminPull:
    if runtime.garmin is None:
        typer.echo("Garmin entegrasyonu bu çalışma zamanında yok.", err=True)
        raise typer.Exit(2)
    return runtime.garmin


def garmin_status() -> None:
    """Show Garmin Connect Personal API pull status without health values or secrets."""
    runtime = context.runtime()

    async def command():
        try:
            return await _garmin(runtime).status()
        finally:
            await runtime.shutdown()

    typer.echo(json.dumps(context.run(command()), indent=2, default=str))


def garmin_pull(kind: list[str] | None = KINDS_OPTION) -> None:
    """Pull the latest GARMIN_LOOKBACK_DAYS once, as the scheduler does."""
    runtime = context.runtime()

    async def command():
        try:
            return await _garmin(runtime).run(kind or None)
        finally:
            await runtime.shutdown()

    summary = context.run(command())
    typer.echo(json.dumps(summary.as_dict(), indent=2, default=str))
    if summary.failed:
        raise typer.Exit(1)


def garmin_backfill(
    since: str = typer.Option(..., "--since", help="First day, YYYY-MM-DD."),
    until: str | None = typer.Option(None, "--until", help="Last day (inclusive); default: today."),
    kind: list[str] | None = KINDS_OPTION,
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Count requests only; make no network requests or writes."
    ),
) -> None:
    """Backfill Garmin history in chunks; resume with the same bounds after interruption."""
    try:
        start = date.fromisoformat(since)
        stop = date.fromisoformat(until) if until else None
    except ValueError as exc:
        typer.echo(f"Tarih biçimi YYYY-AA-GG olmalı: {exc}", err=True)
        raise typer.Exit(2) from None
    if stop is not None and stop < start:
        typer.echo("--until, --since gününden önce olamaz.", err=True)
        raise typer.Exit(2)
    runtime = context.runtime()

    async def command():
        try:
            return await _garmin(runtime).backfill(start, stop, kinds=kind or None, dry_run=dry_run)
        finally:
            await runtime.shutdown()

    report = context.run(command())
    typer.echo(json.dumps(report, indent=2, default=str))
    if report.get("failed"):
        raise typer.Exit(1)
