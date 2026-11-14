"""Object construction for unambiguous Garmin response JSON."""

from typing import Any


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate decoded names before a response can become a record.

    The JSON decoder calls this for every object, including objects inside
    arrays, after resolving string escapes. Each call owns its dictionary, so
    sibling objects may reuse names. Values never determine uniqueness.
    Raising ValueError uses the client's existing safe decode-error boundary;
    neither the duplicate name nor its values belong in the exception message.
    """
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result
