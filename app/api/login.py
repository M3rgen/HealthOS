"""Self-contained login page that exchanges APP_UI_TOKEN for a session cookie, and logout."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import time
from collections.abc import Callable
from html import escape
from ipaddress import IPv6Address, ip_address
from urllib.parse import parse_qs, urlencode

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.core.auth import (
    COOKIE_MAX_AGE,
    COOKIE_NAME,
    LOGIN_PATH,
    LOGOUT_PATH,
    SessionStore,
    request_authenticated,
    safe_next,
    same_origin_form,
    session_cookies,
    token_matches,
)
from app.core.canonical_host import isolated_url, redirect_enabled
from app.core.cookie_security import request_scheme, secure_cookie
from app.core.host_validation import LOCAL_HOSTS, hostname
from app.core.logging import event

logger = logging.getLogger(__name__)
router = APIRouter(include_in_schema=False)
MAX_FORM_BYTES = 8 * 1024
# A small constant pause on a wrong token; the token's length makes guessing hopeless
# anyway, this only keeps a script from hammering the form at full speed.
FAILED_LOGIN_DELAY_SECONDS = 0.5
# Attempts allowed through without waiting, one per interval after that, one queue for the whole
# application. Beyond MAX_WAIT an attempt is answered at once with 429 and its token is not read.
LOGIN_ATTEMPT_INTERVAL_SECONDS = 0.5
LOGIN_BURST_ATTEMPTS = 5
LOGIN_MAX_WAIT_SECONDS = 8
# A cookie cannot be scoped to a port: every other server the browser reaches on plain
# localhost or 127.0.0.1 receives it too. On those hosts it stays valid for a day instead of
# 30 days. healthos.localhost and HTTPS keep the full lifetime.
UNISOLATED_HOST_SESSION_SECONDS = 24 * 3600
UNISOLATED_HOSTS = LOCAL_HOSTS - {"healthos.localhost"}


class LoginThrottle:
    """Bounds password guessing for the whole application, with no state that outlasts a flood.

    Each attempt reserves the next free slot (GCRA) before its token is compared, so concurrent
    guesses cannot share a slot. Nothing is tied to an address or an account: a flood of
    guesses cannot lock anyone out, once it stops the login is free again, and sessions already
    issued and bearer-token API access never pass through here.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.next_free = 0.0

    def reserve(self) -> float | None:
        """Seconds to wait before evaluating this attempt, or None when the queue is full."""
        now = self.clock()
        tolerance = (LOGIN_BURST_ATTEMPTS - 1) * LOGIN_ATTEMPT_INTERVAL_SECONDS
        start = max(now, self.next_free)
        wait = max(0.0, start - now - tolerance)
        if wait > LOGIN_MAX_WAIT_SECONDS:
            return None
        self.next_free = start + LOGIN_ATTEMPT_INTERVAL_SECONDS
        return wait


STYLE = (
    ":root{color-scheme:dark;--page:#181814;--surface:#20201c;--inset:#242420;--line:#373731;"
    "--border:#262421;--text:#f8f8f8;--muted:#b9b7ae;--amber:#f0c478;--ink:#141413;"
    "--error:#ff8a8a}"
    "*{box-sizing:border-box}"
    "html,body{height:100%;margin:0}"
    "body{display:grid;grid-template-rows:minmax(0,1fr);place-items:center;padding:16px;"
    "background:var(--page);color:var(--text);"
    'font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}'
    "main{width:min(380px,100%);background:var(--surface);border:1px solid var(--border);"
    "border-radius:12px;padding:28px 26px 24px;max-height:100%;overflow:auto}"
    "h1{margin:0 0 6px;font-size:28px;font-weight:750;letter-spacing:-1px}"
    "h1 span{font-weight:600;color:var(--amber)}"
    "p{margin:0;color:var(--muted)}"
    "form{display:grid;gap:10px;margin-top:22px}"
    "label{font-size:13px;font-weight:600}"
    "input{width:100%;font:inherit;color:var(--text);background:var(--inset);"
    "border:1px solid var(--line);border-radius:8px;padding:11px 12px}"
    "input:focus-visible,button:focus-visible,a:focus-visible{"
    "outline:2px solid var(--amber);outline-offset:3px}"
    "button{font:inherit;font-weight:650;color:var(--ink);background:var(--amber);border:0;"
    "border-radius:8px;padding:11px 14px;margin-top:6px;cursor:pointer}"
    ".error{color:var(--error);font-size:13px}"
    ".hint{margin-top:16px;font-size:12px}"
    "code{font-size:12px;color:var(--text)}"
    "a{color:var(--amber)}"
    "@media(max-height:460px){main{padding:16px 18px}h1{font-size:22px}form{margin-top:12px;"
    "gap:6px}}"
)
STYLE_HASH = base64.b64encode(hashlib.sha256(STYLE.encode()).digest()).decode()
CSP = (
    f"default-src 'none'; style-src 'sha256-{STYLE_HASH}'; img-src data:; "
    "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
)
ERRORS = {
    "tr": {
        "token": "Erişim anahtarı yanlış. Kontrol edip tekrar dene.",
        "origin": "İstek doğrulanamadı. Sayfayı yenileyip tekrar dene.",
        "rate": "Çok fazla deneme yapıldı. Birkaç saniye bekleyip tekrar dene.",
        "canonical": "Giriş için aşağıdaki adresi aç ve erişim anahtarını orada gir.",
    },
    "en": {
        "token": "The access key is incorrect. Check it and try again.",
        "origin": "The request could not be verified. Refresh the page and try again.",
        "rate": "Too many attempts. Wait a few seconds and try again.",
        "canonical": "Open the address below and enter the access key there to sign in.",
    },
}
TEXT = {
    "tr": {
        "title": "Giriş",
        "intro": "Panele devam etmek için erişim anahtarını gir.",
        "label": "Erişim anahtarı",
        "submit": "Giriş yap",
        "days": "30 gün",
        "hours": "{hours} saat",
        "isolated_before": "Bu adresteki çerez, bilgisayardaki diğer yerel sunuculara da gider.",
        "isolated_after": "adresi kendi çerez alanını kullanır.",
        "key_hint": "Anahtar, sunucudaki <code>APP_UI_TOKEN</code> değeridir.",
        "remember": "Bu tarayıcı, sunucu yeniden başlatılmadıkça seni {duration} hatırlar.",
        "name": "Türkçe",
    },
    "en": {
        "title": "Sign in",
        "intro": "Enter your access key to continue to the dashboard.",
        "label": "Access key",
        "submit": "Sign in",
        "days": "30 days",
        "hours": "{hours} hours",
        "isolated_before": (
            "Cookies at this address are also sent to other local servers on this computer."
        ),
        "isolated_after": "uses its own cookie space.",
        "key_hint": "The key is the server’s <code>APP_UI_TOKEN</code> value.",
        "remember": "This browser remembers you for {duration}, unless the server restarts.",
        "name": "English",
    },
}


def page(
    next_path: str,
    error: str | None = None,
    status_code: int = 200,
    remember_seconds: int = COOKIE_MAX_AGE,
    isolated: str | None = None,
    lang: str = "tr",
) -> HTMLResponse:
    text = TEXT[lang]
    other = "en" if lang == "tr" else "tr"
    language_url = f"{LOGIN_PATH}?{urlencode({'lang': other, 'next': safe_next(next_path)})}"
    remembered = (
        text["days"]
        if remember_seconds >= COOKIE_MAX_AGE
        else text["hours"].format(hours=remember_seconds // 3600)
    )
    message = f'<p class="error" role="alert">{escape(ERRORS[lang][error])}</p>' if error else ""
    if isolated:
        # Only on plain localhost/127.0.0.1: the same login with a cookie jar of its own.
        message += (
            f'<p class="hint">{text["isolated_before"]} '
            f'<a href="{escape(isolated, quote=True)}">healthos.localhost</a> '
            f"{text['isolated_after']}</p>"
        )
    html = f"""<!doctype html>
<html lang="{lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#181814">
<meta name="robots" content="noindex">
<link rel="icon" href="data:,">
<title>HealthOS · {text["title"]}</title>
<style>{STYLE}</style>
</head>
<body>
<main>
<h1>Health<span>OS</span></h1>
<p>{text["intro"]}</p>
<form method="post" action="{LOGIN_PATH}">
<input type="hidden" name="next" value="{escape(next_path, quote=True)}">
<input type="hidden" name="lang" value="{lang}">
<label for="token">{text["label"]}</label>
<input id="token" name="token" type="password" autocomplete="current-password"
 autocapitalize="off" spellcheck="false" required autofocus>
{message}
<button type="submit">{text["submit"]}</button>
</form>
<p class="hint">{text["key_hint"]}
{text["remember"].format(duration=remembered)}</p>
<p class="hint"><a href="{escape(language_url, quote=True)}" hreflang="{other}"
 lang="{other}">{TEXT[other]["name"]}</a></p>
</main>
</body>
</html>
"""
    return HTMLResponse(
        html,
        status_code=status_code,
        headers={"Content-Security-Policy": CSP, "Cache-Control": "no-store"},
    )


def _token(request: Request) -> str | None:
    return request.app.state.settings.app_ui_token


def _sessions(request: Request) -> SessionStore:
    return request.app.state.ui_sessions


def _throttle(request: Request) -> LoginThrottle:
    throttle = getattr(request.app.state, "login_throttle", None)
    if throttle is None:
        throttle = request.app.state.login_throttle = LoginThrottle()
    return throttle


def _session_seconds(request: Request) -> int:
    """How long this login may last on the host it arrived at (see UNISOLATED_HOSTS)."""
    if secure_cookie(request, request.app.state.settings):
        return COOKIE_MAX_AGE
    hosts = request.headers.getlist("host")
    name = hostname(hosts[0]) if len(hosts) == 1 else None
    return UNISOLATED_HOST_SESSION_SECONDS if name in UNISOLATED_HOSTS else COOKIE_MAX_AGE


def _language(request: Request, lang: str | None) -> str:
    # Literals rather than the received value: only these two strings can reach the page.
    if lang == "tr":
        return "tr"
    if lang == "en":
        return "en"
    first = request.headers.get("accept-language", "").split(",", 1)[0].split(";", 1)[0]
    return "en" if first.strip().split("-", 1)[0].lower() == "en" else "tr"


@router.api_route(LOGIN_PATH, methods=["GET", "HEAD"])
async def login_page(
    request: Request, next_path: str | None = Query(None, alias="next"), lang: str | None = None
) -> Response:
    target = safe_next(next_path)
    if request_authenticated(_token(request), request.headers, _sessions(request)):
        return RedirectResponse(target, status_code=303, headers={"Cache-Control": "no-store"})
    return page(
        target,
        remember_seconds=_session_seconds(request),
        isolated=_isolated_login(request, target),
        lang=_language(request, lang),
    )


def _isolated_login(request: Request, target: str) -> str | None:
    """This login on healthos.localhost, offered where the session cookie is shared."""
    hosts = request.headers.getlist("host")
    if len(hosts) != 1 or secure_cookie(request, request.app.state.settings):
        return None
    path = f"{LOGIN_PATH}?{urlencode({'next': target})}"
    return isolated_url(hosts[0], path, shared=UNISOLATED_HOSTS)


@router.post(LOGIN_PATH)
async def login(request: Request) -> Response:
    response = await _login(request)
    event(
        logger,
        "LOGIN_ATTEMPT",
        client_class=_client_class(request),
        result="success" if response.status_code == 303 else "failure",
    )
    return response


def _client_class(request: Request) -> str:
    """Only the connection peer; forwarding headers and raw addresses never enter the audit."""
    try:
        address = ip_address(request.client.host) if request.client else None
    except ValueError:
        address = None
    if address is None:
        return "unknown"
    if isinstance(address, IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return "loopback" if address.is_loopback else "non_loopback"


async def _login(request: Request) -> Response:
    token = _token(request)
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_FORM_BYTES:
            return Response("Form too large", status_code=413)
    try:
        form = parse_qs(body.decode(), max_num_fields=8)
    except (UnicodeDecodeError, ValueError):
        form = {}
    target = safe_next((form.get("next") or ["/"])[0])
    lang = _language(request, (form.get("lang") or [None])[0])
    if not same_origin_form(request.headers, request_scheme(request, request.app.state.settings)):
        return page(target, "origin", 403, lang=lang)
    if not token:
        return RedirectResponse(target, status_code=303)
    wait = _throttle(request).reserve()
    if wait is None:
        response = page(target, "rate", 429, lang=lang)
        response.headers["Retry-After"] = str(int(LOGIN_MAX_WAIT_SECONDS))
        return response
    if wait:
        await asyncio.sleep(wait)
    if not token_matches(token, (form.get("token") or [""])[0]):
        await asyncio.sleep(FAILED_LOGIN_DELAY_SECONDS)
        return page(target, "token", 401, lang=lang)
    # A correct key must not create a session on a cookie domain shared with other apps.
    # Keep explicit interface addresses and the existing secure-cookie policy unchanged.
    settings = request.app.state.settings
    if redirect_enabled(settings.app_public_url) and hostname(request.headers.get("host", "")) in {
        "localhost",
        "127.0.0.1",
    }:
        isolated = _isolated_login(request, target)
        if isolated is not None:
            return HTMLResponse(
                f'<!doctype html><html lang="{lang}"><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width, initial-scale=1">'
                f"<title>HealthOS</title><p>{ERRORS[lang]['canonical']}</p>"
                f'<a href="{escape(isolated, quote=True)}">healthos.localhost</a></html>',
                status_code=409,
                headers={"Cache-Control": "no-store", "Location": isolated},
            )
    response = RedirectResponse(target, status_code=303, headers={"Cache-Control": "no-store"})
    lifetime = _session_seconds(request)
    response.set_cookie(
        COOKIE_NAME,
        _sessions(request).issue(ttl_seconds=lifetime),
        max_age=lifetime,
        path="/",
        httponly=True,
        samesite="strict",
        secure=secure_cookie(request, request.app.state.settings),
    )
    return response


@router.post(LOGOUT_PATH)
async def logout(request: Request) -> Response:
    """Revoke the browser's session and clear its cookie (exempt from the token check)."""
    if not same_origin_form(request.headers, request_scheme(request, request.app.state.settings)):
        return Response("Cross-origin logout refused", status_code=403)
    sessions = _sessions(request)
    for session_id in session_cookies(request.headers):
        sessions.revoke(session_id)
    response = RedirectResponse(LOGIN_PATH, status_code=303, headers={"Cache-Control": "no-store"})
    response.delete_cookie(
        COOKIE_NAME,
        path="/",
        httponly=True,
        samesite="strict",
        secure=secure_cookie(request, request.app.state.settings),
    )
    return response
