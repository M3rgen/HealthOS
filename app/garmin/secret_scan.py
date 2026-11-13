"""Credential detection with bounded, nonrecursive work even on malformed JSON."""

import json
import re
from collections.abc import Iterator

_SPECIAL = re.compile(r'["\\\x00-\x1f]')
_ESCAPE = re.compile(r'\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4})')


def json_strings(text: str) -> Iterator[str]:
    """Decode only complete string literals, each from a disjoint input slice.

    The special-character iterator advances once through the text; escape checks
    inspect at most six characters. It does not validate the surrounding JSON, so
    duplicate keys, deep structures and readable strings in broken bodies count.

    After an invalid escape/control character, discard the candidate and recover
    at the next quote. Any escaped quote within the discarded prefix would hit
    the same failure, so retrying there cannot recover another valid string.
    Likewise an unterminated candidate and its escaped quotes need no decoding.
    """
    start: int | None = None
    skip_until = 0
    for special in _SPECIAL.finditer(text):
        index = special.start()
        if index < skip_until:
            continue
        char = special.group()
        if char == '"':
            if start is None:
                start = index
            else:
                # The lexer proved this slice valid. Never give a decoder an
                # unbounded suffix at every quote in an unterminated string.
                yield json.loads(text[start : index + 1])
                start = None
        elif start is not None:
            escape = _ESCAPE.match(text, index) if char == "\\" else None
            if escape is None:
                start = None
            else:
                skip_until = escape.end()
