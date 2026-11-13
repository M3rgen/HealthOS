"""Script token responses through the real transport, including secret echoes."""

import base64

import httpx
import pytest
from garmin_records import TOKEN
from garmin_token_leaks import SecretEchoGarmin

from app.garmin.client import TOKEN_PATH

RENEWED_TOKEN = TOKEN + "-renewed"


@pytest.fixture(
    params=[b"\xff", "é".encode(), "🙂".encode(), *[bytes([c]) for c in (*range(32), 127)]],
    ids=lambda value: value.hex(),
)
def invalid_token_body(request):
    """Non-ASCII, malformed UTF-8 and every ASCII control inside a synthetic bearer."""
    return b"synthetic-invalid-bearer-prefix" + request.param + b"-suffix"


class GetTransportGarmin(SecretEchoGarmin):
    """Echo the actual bearer header in GET errors, rotating it after a 401."""

    def __init__(self):
        super().__init__()
        self.failures: dict[str, list[type[httpx.HTTPError]]] = {}

    def __call__(self, request):
        path = request.url.path
        if path == TOKEN_PATH and self.token_requests:
            self.token = RENEWED_TOKEN
        failures = self.failures.get(path)
        if request.method == "GET" and not self.statuses.get(path) and failures:
            self.requests.append(request)
            raise failures.pop(0)(
                f"transport echo {request.headers['authorization']}", request=request
            )
        return super().__call__(request)


class RetryingGarmin(SecretEchoGarmin):
    def __init__(self, outcomes):
        super().__init__()
        self.outcomes = list(outcomes)
        self.token_error_body = f"temporary failure; previous bearer {TOKEN}".encode()
        self.token_headers = {"www-authenticate": f"Bearer {TOKEN}"}

    def __call__(self, request):
        if request.url.path == TOKEN_PATH and self.outcomes:
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, int):
                self.token_status = outcome
            else:
                self.requests.append(request)
                self.token_requests += 1
                raise outcome(f"transport echo {request.headers['authorization']}", request=request)
        return super().__call__(request)


@pytest.fixture(
    params={
        "valid": None,
        "positive-overflow": b'{"exp":' + str(10**100).encode() + b"}",
        "negative-overflow": b'{"exp":' + str(-(10**100)).encode() + b"}",
        "nesting": b"[" * 10000 + b"0" + b"]" * 10000,
        "syntax": b'{"exp":',
        "encoding": b"\xff",
    }.items(),
    ids=lambda case: case[0],
)
def token_variant(request):
    payload = request.param[1]
    return TOKEN if payload is None else f"test.{base64.urlsafe_b64encode(payload).decode()}.sig"


@pytest.fixture(
    params=[
        "Thu, 12 Nov 2026 12:00:00 -0000",
        "Thu, 12 Nov 2026 12:00:00",
        "Thu, 32 Nov 2026 12:00:00 GMT",
        "Fri, 31 Dec 999999999999 23:59:59 GMT",
        "NaN",
        "inf",
        "-Infinity",
        "1e309",
    ]
)
def bad_retry_after(request):
    return request.param
