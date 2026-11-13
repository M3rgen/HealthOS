"""Machine-readable Google failures, independent of diagnostic wording."""

from enum import StrEnum


class GoogleHealthErrorCode(StrEnum):
    CONNECTION = "connection"
    WIRE_RESPONSE_TOO_LARGE = "wire_response_too_large"
    RESPONSE_TOO_LARGE = "response_too_large"
    UNSUPPORTED_ENCODING = "unsupported_encoding"
    INVALID_ENCODING = "invalid_encoding"
    INVALID_JSON = "invalid_json"
    INVALID_RESPONSE = "invalid_response"
    INVALID_PAGE_TOKEN = "invalid_page_token"


class GoogleHealthError(RuntimeError):
    default_code: GoogleHealthErrorCode | None = None

    def __init__(
        self,
        message: str = "",
        *,
        http_status: int | None = None,
        code: GoogleHealthErrorCode | None = None,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.code = self.default_code if code is None else code


class ResponseBodyTooLargeError(GoogleHealthError):
    """A raw receive limit, independent of compression and never retried."""

    def __init__(self) -> None:
        super().__init__(
            "Google Health response exceeded its wire byte budget",
            code=GoogleHealthErrorCode.WIRE_RESPONSE_TOO_LARGE,
        )


class ResponseStreamError(GoogleHealthError):
    """An unexpected stream failure, never retried; only its class reaches the archive."""

    def __init__(self) -> None:
        super().__init__("Google Health response stream failed")


class LocalRequestRejectedError(GoogleHealthError):
    def __init__(self) -> None:
        super().__init__("Google Health request was rejected before sending")


class RetryDeferredError(GoogleHealthError):
    """Retry-After exceeds one request's budget: no retry before Google permits it."""


class InvalidPageTokenError(GoogleHealthError):
    default_code = GoogleHealthErrorCode.INVALID_PAGE_TOKEN
