"""Garmin activity identities shared by archival and projection writers.

Short identifiers retain their original keys. Long identifiers use all 256 digest
bits in a URL-safe encoding, leaving room for the namespace within the 64-character
raw key column. The original identifier remains in the immutable response and body.
"""

import hashlib
from base64 import urlsafe_b64encode
from typing import Any

from sqlalchemy import String, and_, case, cast, func


def activity_identifier_sql(body):
    """Decode strings but keep integer JSON text exact, even beyond 64 digits.

    Extracting a JSON number as an SQL number can round it before a text cast.
    The JSON arrow retains its original digits; only strings need unquoting.
    """
    text = cast(body.op("->")("activityId"), String)
    integer = and_(
        func.substr(text, 1, 1).in_(tuple("0123456789-")),
        ~text.contains("."),
        ~text.contains("e"),
        ~text.contains("E"),
    )
    return case(
        (text.startswith('"'), cast(body["activityId"].as_string(), String)),
        (integer, text),
        else_=None,
    )


def activity_identifier(record: Any) -> str | None:
    identifier = record.get("activityId") if isinstance(record, dict) else None
    if not isinstance(identifier, int | str) or isinstance(identifier, bool):
        return None
    return str(identifier)


def activity_key(record: Any) -> str | None:
    identifier = activity_identifier(record)
    if identifier is None or len(identifier) <= 64:
        return identifier
    digest = hashlib.sha256(identifier.encode("utf-8", errors="surrogatepass")).digest()
    return "sha256:" + urlsafe_b64encode(digest).decode("ascii").rstrip("=")
