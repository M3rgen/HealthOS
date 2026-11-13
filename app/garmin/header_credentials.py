"""Validate file and response credentials before constructing HTTP headers.

Validation never interpolates a credential into diagnostics. Callers retain
responsibility for masking the HTTP evidence and choosing the public error.
"""


def is_header_value(value: str) -> bool:
    """Accept only nonempty printable ASCII, excluding DEL and all controls.

    Outer whitespace is stripped by the caller, preserving its input contract.
    Spaces are printable; the API key caller also forbids internal whitespace.
    No JWT syntax or claims are required for a bearer to be safe in a header.
    """
    return bool(value) and value.isascii() and value.isprintable()


def decode_bearer(body: bytes) -> str | None:
    """Decode strictly, returning only a header-safe bearer or a failure marker.

    A decoding exception retains the original bytes in its ``object`` attribute.
    Return a marker rather than an exception so callers can raise their public
    error outside the handler, without a cause or implicit exception context.
    This value must be validated before either half of the cache is published.
    """
    try:
        value = body.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    return value if is_header_value(value) else None
