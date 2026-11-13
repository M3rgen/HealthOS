from __future__ import annotations

import hashlib
import re
import secrets
import time
from collections.abc import Callable
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from starlette.datastructures import URL

from app.api.deps import runtime as runtime_dep
from app.api.deps import settings as settings_dep
from app.core.config import Settings
from app.core.cookie_security import secure_cookie
from app.core.oauth_keys import GoogleReauthorizationRequired
from app.core.oauth_state import COOKIE_NAME, COOKIE_PATH, OAuthStateError, OAuthStateUnavailable
from app.google_health.data_types import readonly_scopes
from app.runtime import Runtime
from app.schemas.status import Connected
from app.services.providers.credentials import GoogleAccountConflict

router = APIRouter(prefix="/auth/google", tags=["auth"])
HANDOFF_PATH = "/auth/google/handoff"
HANDOFF_SECONDS = 120


class HandoffTickets:
    """Single-use tickets that carry an authenticated start to the callback origin.

    Google calls back to the registered redirect URI (``localhost`` by default), while the
    interface lives on ``healthos.localhost``, whose cookies other local servers never see. A
    start there issues a ticket instead of asking for a second login on ``localhost``: the
    callback origin gets only the OAuth binding cookie, minted when the ticket is spent. Only a
    hash of each ticket is kept; the oldest is dropped when the store is full, so requests
    cannot crowd out the user's own start.
    """

    MAX_PENDING = 16

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self._expiry: dict[str, float] = {}

    def issue(self) -> str:
        now = self.clock()
        self._expiry = {key: expiry for key, expiry in self._expiry.items() if expiry > now}
        while len(self._expiry) >= self.MAX_PENDING:
            del self._expiry[min(self._expiry, key=self._expiry.__getitem__)]
        ticket = secrets.token_urlsafe(32)
        self._expiry[hashlib.sha256(ticket.encode()).hexdigest()] = now + HANDOFF_SECONDS
        return ticket

    def spend(self, ticket: str) -> bool:
        expiry = self._expiry.pop(hashlib.sha256(ticket.encode()).hexdigest(), None)
        return expiry is not None and expiry > self.clock()


def _origin(url: URL) -> tuple[str, str | None, int | None]:
    return url.scheme, url.hostname, url.port or {"http": 80, "https": 443}.get(url.scheme)


def _tickets(request: Request) -> HandoffTickets:
    tickets = getattr(request.app.state, "oauth_handoffs", None)
    if tickets is None:
        tickets = request.app.state.oauth_handoffs = HandoffTickets()
    return tickets


@router.get("/start")
async def start(
    request: Request,
    settings: Settings = Depends(settings_dep),
    runtime: Runtime = Depends(runtime_dep),
):
    if not settings.credentials_configured:
        raise HTTPException(503, "Google OAuth client credentials are not configured")
    redirect = URL(settings.google_redirect_uri)
    if _origin(request.url) != _origin(redirect):
        # Mint the cookie and state only on the configured callback origin, reached with a
        # ticket so that no UI login (and no UI cookie) is ever needed there.
        query = urlencode({"ticket": _tickets(request).issue()})
        return RedirectResponse(
            str(redirect.replace(path=HANDOFF_PATH, query=query, fragment="")),
            status_code=303,
            headers={"Cache-Control": "no-store"},
        )
    return _begin(request, settings, runtime)


@router.get("/handoff")
async def handoff(
    request: Request,
    ticket: str = Query("", max_length=128),
    settings: Settings = Depends(settings_dep),
    runtime: Runtime = Depends(runtime_dep),
):
    """Exempt from the UI token: the ticket is the proof of an authenticated start."""
    if not settings.credentials_configured:
        raise HTTPException(503, "Google OAuth client credentials are not configured")
    on_callback_origin = _origin(request.url) == _origin(URL(settings.google_redirect_uri))
    if not (on_callback_origin and ticket and _tickets(request).spend(ticket)):
        raise HTTPException(
            400,
            "Google bağlantısı başlatılamadı: bağlantının süresi dolmuş ya da kullanılmış. "
            "Bağlantıyı panelden yeniden başlatın.",
        )
    return _begin(request, settings, runtime)


def _begin(request: Request, settings: Settings, runtime: Runtime) -> RedirectResponse:
    cookie = request.cookies.get(COOKIE_NAME, "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", cookie):
        cookie = secrets.token_urlsafe(32)
    code_verifier = secrets.token_urlsafe(32)
    try:
        state = request.app.state.oauth_states.issue(settings, cookie, code_verifier=code_verifier)
    except OAuthStateUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    url = runtime.oauth.authorization_url(
        readonly_scopes(settings.extra_data_types, settings.enabled_data_types),
        state,
        code_verifier=code_verifier,
    )
    response = RedirectResponse(url, headers={"Cache-Control": "no-store"})
    # Lax permits Google's top-level GET redirect. The UI login cookie stays Strict.
    response.set_cookie(
        COOKIE_NAME,
        cookie,
        path=COOKIE_PATH,
        httponly=True,
        samesite="lax",
        secure=secure_cookie(request, settings),
    )
    return response


@router.get("/callback", response_model=Connected)
async def callback(
    request: Request,
    code: str = Query(...),
    state: str = Query(...),
    settings: Settings = Depends(settings_dep),
    runtime: Runtime = Depends(runtime_dep),
) -> Connected:
    cookie = request.cookies.get(COOKIE_NAME)
    if not cookie:
        raise HTTPException(
            400,
            "Google bağlantısının oturum çerezi bulunamadı. "
            "Bağlantıyı aynı adresten yeniden başlatın.",
        )
    try:
        code_verifier = request.app.state.oauth_states.consume(settings, state, cookie)
    except OAuthStateError as exc:
        raise HTTPException(400, str(exc)) from exc
    token = await runtime.oauth.exchange_code(code, code_verifier=code_verifier)
    # Verify the candidate account without using or mutating the active token cache.
    identity = await runtime.google.get_identity(access_token=token["access_token"])
    health_user_id = identity.get("healthUserId")
    if not isinstance(health_user_id, str) or not health_user_id:
        raise HTTPException(502, "Google Health did not return a valid account identity")
    async with runtime.tokens.credential_update():
        try:
            await runtime.credentials.check_account(health_user_id)
        except GoogleAccountConflict as exc:
            raise HTTPException(409, str(exc)) from None
        refresh_token = token.get("refresh_token")
        scopes = token.get("scope")
        require_current = not refresh_token
        if not refresh_token:
            try:
                refresh_token = await runtime.credentials.load_refresh_token()
            except GoogleReauthorizationRequired as exc:
                raise HTTPException(400, str(exc)) from None
            if not refresh_token:
                raise HTTPException(
                    400, "Google did not return an offline refresh token; revoke and retry consent"
                )
            # An existing offline token is reusable only for this verified account.
            existing = await runtime.oauth.refresh(refresh_token)
            existing_identity = await runtime.google.get_identity(access_token=existing.value)
            if existing_identity.get("healthUserId") != health_user_id:
                raise HTTPException(
                    400, "Google did not return an offline token for this account; retry consent"
                )
            refresh_token = existing.replacement_refresh_token or refresh_token
            scopes = existing.scopes or scopes
        try:
            await runtime.credentials.save_connection(
                refresh_token,
                scopes,
                health_user_id,
                identity.get("legacyUserId"),
                require_current=require_current,
            )
        except GoogleAccountConflict as exc:
            raise HTTPException(409, str(exc)) from None
        except GoogleReauthorizationRequired as exc:
            raise HTTPException(400, str(exc)) from None
    return {"status": "connected", "health_user_id": health_user_id}
