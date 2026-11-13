"""Explicit Google account changes, separate from browser reconnects."""

import typer

from app.cli._context import context
from app.services.providers.credentials import CredentialStoreError


def google_rebind(
    confirm_account_change: bool = typer.Option(
        False,
        "--confirm-account-change",
        help="Explicitly confirm changing the Google account while preserving the archive.",
    ),
) -> None:
    """Change the Google account using a refresh token."""
    if not confirm_account_change:
        typer.echo("Hesap değişimi için --confirm-account-change gerekli.", err=True)
        raise typer.Exit(2)
    refresh_token = typer.prompt("Yeni Google hesabının yenileme token'ı", hide_input=True)
    if not refresh_token.strip():
        typer.echo("Yenileme token'ı boş olamaz.", err=True)
        raise typer.Exit(2)

    async def command():
        try:
            candidate = await runtime.oauth.refresh(refresh_token)
            identity = await runtime.google.get_identity(access_token=candidate.value)
            account = identity.get("healthUserId")
            if not isinstance(account, str) or not account:
                raise CredentialStoreError("Invalid Google account identity")
            async with runtime.tokens.credential_update():
                await runtime.credentials.rebind_connection(
                    candidate.replacement_refresh_token or refresh_token,
                    candidate.scopes,
                    account,
                    identity.get("legacyUserId"),
                )
        finally:
            await runtime.shutdown()

    try:
        runtime = context.runtime()
        context.run(command())
    except Exception:
        # Typer's diagnostic locals would expose the interactively entered token.
        typer.echo("Google bağlantısı tamamlanamadı.", err=True)
        raise typer.Exit(1) from None
    typer.echo("Google bağlantısı doğrulandı; hesap değiştiyse geçmişe kaydedildi. Arşiv korundu.")
