"""Transport policy shared by session cookies and same-origin checks."""

from starlette.requests import Request

from app.core.config import Settings


def request_scheme(request: Request, settings: Settings) -> str:
    # Direct TLS cannot be downgraded by a forwarded header. Do not interpret proxy
    # chains or duplicate headers: the trusted proxy must replace the incoming value.
    if request.url.scheme == "https":
        return "https"
    if settings.app_trust_proxy_headers:
        forwarded = request.headers.getlist("x-forwarded-proto")
        if len(forwarded) == 1 and forwarded[0].strip().lower() == "https":
            return "https"
    return request.url.scheme


def secure_cookie(request: Request, settings: Settings) -> bool:
    return request_scheme(request, settings) == "https" or (
        settings.app_public_url or ""
    ).lower().startswith("https://")
