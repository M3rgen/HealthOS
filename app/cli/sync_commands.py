"""Synchronization status and runtime-backed archive updates."""

from __future__ import annotations

import json

import typer

from app.cli._context import context
from app.services.synchronization.sync_reporting import failed_backfills, sync_status


def status() -> None:
    """Show synchronization and backfill status."""

    async def command():
        async with context.sessions() as session:
            return await sync_status(session)

    typer.echo(json.dumps(context.run(command()), indent=2))


def backfill(
    data_type: str | None = typer.Option(None, "--data-type"),
    reset_abandoned: bool = typer.Option(
        False,
        "--reset-abandoned",
        help="Requeue abandoned windows (of --data-type, or all) with a fresh retry count first.",
    ),
) -> None:
    """Start or resume the historical backfill and wait for completion."""
    runtime = context.runtime(require_google=True)

    async def command():
        try:
            if reset_abandoned:
                reset = await runtime.backfill.reset_abandoned([data_type] if data_type else None)
                typer.echo(f"Requeued {reset} abandoned backfill window(s)")
            completed = await runtime.backfill.run([data_type] if data_type else None)
            async with context.sessions() as session:
                failed = await failed_backfills(session, data_type)
            return completed, failed
        finally:
            await runtime.shutdown()

    completed, failed = context.run(command())
    typer.echo(f"Completed {completed} backfill windows")
    for job in failed:
        typer.echo(
            f"backfill {job['status']}: {job['data_type']} "
            f"{job['window_start']}: {job['last_error']}",
            err=True,
        )
    if failed:
        raise typer.Exit(1)


def sync(data_type: str | None = typer.Argument(None)) -> None:
    """Run incremental synchronization for one or every configured data type."""
    runtime = context.runtime(require_google=True)

    async def command():
        try:
            if data_type:
                return await runtime.incremental.run(data_type)
            return await runtime.incremental.run_all()
        finally:
            await runtime.shutdown()

    result = context.run(command())
    typer.echo(str(result))
    if isinstance(result, dict) and any(isinstance(item, Exception) for item in result.values()):
        raise typer.Exit(1)


def reconcile(days: int = typer.Option(7, min=1, max=3_650)) -> None:
    """Re-read a recent window across every mirrored data type."""
    runtime = context.runtime(require_google=True)

    async def command():
        try:
            return await runtime.reconciliation.run(days)
        finally:
            await runtime.shutdown()

    result = context.run(command())
    typer.echo(str(result))
    if any(isinstance(item, Exception) for item in result.values()):
        raise typer.Exit(1)


def probe(days: int = typer.Option(30, min=1, max=365)) -> None:
    """Probe accessible Google Health types without archiving records or a backfill.

    The probe's Google responses are kept in raw_api_responses (source "probe").
    """
    runtime = context.runtime(require_google=True)

    async def command():
        try:
            return await runtime.discovery.probe(days)
        finally:
            await runtime.shutdown()

    typer.echo(json.dumps(context.run(command()), indent=2, default=str))
