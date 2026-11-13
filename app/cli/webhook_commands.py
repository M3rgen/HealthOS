"""Webhook readiness, receipt status and explicit pending replay."""

from __future__ import annotations

import json

import typer

from app.cli._context import context
from app.services.synchronization.webhook_reporting import webhook_status as receipt_status


def webhook_readiness() -> None:
    """Report secret-safe Google Health subscriber prerequisites."""
    typer.echo(json.dumps(context.settings().webhook_readiness, indent=2))


def webhook_status() -> None:
    """Show secret-safe local webhook receipt and processing status."""

    async def command():
        async with context.sessions() as session:
            report = await receipt_status(session)
        return {"readiness": context.settings().webhook_readiness, **report}

    typer.echo(json.dumps(context.run(command()), indent=2))


def webhook_replay_pending() -> None:
    """Retry stored pending and failed webhook elements without a Google resend.

    Reuse verified archived batches; completed elements are not fetched again.
    """

    runtime = context.runtime()

    async def command():
        try:
            return await runtime.webhook_processor.process_pending()
        finally:
            await runtime.shutdown()

    result = context.run(command())
    typer.echo(json.dumps(result.__dict__, indent=2))
    if result.failed_elements:
        raise typer.Exit(1)
