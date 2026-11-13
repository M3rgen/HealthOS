"""Keep unexpected command failures out of Typer's traceback renderer."""

import logging

import typer
from typer import Abort, Exit

# Typer 0.27 vendors Click without exporting its base error/context. typer.Context is
# a narrower subclass, so cannot type an override of TyperGroup.invoke. Keep these
# compatibility imports in this adapter; external Click classes have different identities.
from typer._click.core import Context
from typer._click.exceptions import ClickException
from typer.core import TyperGroup

from app.core.logging import RedactSecrets

logger = logging.getLogger(__name__)
# Filter before propagation, including Python's fallback stderr log handler when
# the CLI runs without the web application's logging configuration.
logger.addFilter(RedactSecrets(inline=True))


class SafeTyperGroup(TyperGroup):
    def invoke(self, ctx: Context):
        try:
            return super().invoke(ctx)
        except (ClickException, Exit, Abort):
            # Preserve usage errors, intentional command exits and cancellation.
            raise
        except Exception as exc:
            logger.error("CLI command failed", exc_info=True)
            typer.echo(f"Komut tamamlanamadı. Hata türü: {type(exc).__name__}", err=True)
            raise typer.Exit(1) from None
