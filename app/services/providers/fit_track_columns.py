"""Stream one column of a stored track package without decoding the whole package.

A method 1 package column set is a zlib-compressed JSON object of equal-length arrays
(``fit_tracks.Columns.pack``): ``{"time":[0,4,…],"latitude":[…],…}``. Values are JSON
numbers, ``null`` or, for ``action``, a short quoted word, so no value contains ``,``,
``]`` or ``":[``. Each reader decompresses the package again in bounded pieces and
holds one piece plus one partial token; nothing proportional to the sample count.
"""

import math
import re
import zlib
from collections.abc import Iterable, Iterator

INPUT_PIECE = 16 * 1024
# Small pieces bound each reader's token list: a constant channel decompresses to
# thousands of two-character values per kilobyte of input.
OUTPUT_PIECE = 4 * 1024
_NAME = re.compile(r'"([a-z_]{1,32})":\[')
_TOKEN = re.compile(r'-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?|null|"(?:pause|resume)"')
_NAME_TAIL = 40


class TrackFormatError(ValueError):
    """A stored package is not in the method's format. The message never holds a value."""

    def __init__(self) -> None:
        super().__init__("İz paketi okunamadı / Track package unreadable")


def pieces(blob: bytes) -> Iterator[str]:
    """Decompressed text in pieces of at most ``OUTPUT_PIECE`` characters."""
    stream, view = zlib.decompressobj(), memoryview(blob)
    try:
        for start in range(0, len(view), INPUT_PIECE):
            data: bytes | memoryview = view[start : start + INPUT_PIECE]
            while data:
                text = stream.decompress(data, OUTPUT_PIECE)
                data = stream.unconsumed_tail
                if text:
                    yield text.decode("ascii")
        if tail := stream.flush():
            yield tail.decode("ascii")
    except (zlib.error, UnicodeDecodeError):
        raise TrackFormatError() from None


def column_names(blob: bytes) -> list[str]:
    """The column names in stored order; a name split across pieces is still found."""
    names: list[str] = []
    carry = ""
    for piece in pieces(blob):
        text = carry + piece
        end = 0
        for match in _NAME.finditer(text):
            names.append(match.group(1))
            end = match.end()
        carry = text[max(end, len(text) - _NAME_TAIL) :]
    return names


def tokens(blob: bytes, name: str) -> Iterator[str]:
    """The raw JSON tokens of one column, in order; nothing when the column is absent."""
    marker, carry, inside = f'"{name}":[', "", False
    for piece in pieces(blob):
        text = carry + piece
        if not inside:
            at = text.find(marker)
            if at < 0:
                carry = text[-len(marker) :]
                continue
            inside, text = True, text[at + len(marker) :]
        end = text.find("]")
        if end >= 0:
            if body := text[:end]:
                yield from body.split(",")
            return
        parts = text.split(",")
        carry = parts.pop()
        yield from parts
    if inside:
        raise TrackFormatError()


def number(token: str) -> float:
    """A stored numeric token as a float; ``null`` is NaN."""
    if token == "null":
        return math.nan
    try:
        return float(token)
    except ValueError:
        raise TrackFormatError() from None  # The token itself stays out of the message.


def values(blob: bytes, name: str) -> Iterator[float]:
    return (number(token) for token in tokens(blob, name))


def json_array(items: Iterable[str]) -> Iterator[str]:
    """``[a,b,…]`` in bounded text chunks."""
    chunk: list[str] = []
    yield "["
    first = True
    for item in items:
        chunk.append(item)
        if len(chunk) == 1024:
            yield ("" if first else ",") + ",".join(chunk)
            chunk, first = [], False
    if chunk:
        yield ("" if first else ",") + ",".join(chunk)
    yield "]"


def checked(token: str) -> str:
    """A stored token that is valid JSON of the method's value kinds, unchanged."""
    if _TOKEN.fullmatch(token) is None:
        raise TrackFormatError()
    return token


def selected(items: Iterable[str], indices: list[int] | None) -> Iterator[str]:
    """The checked items at sorted ``indices`` (all of them when ``indices`` is None)."""
    if indices is None:
        yield from map(checked, items)
        return
    position = 0
    for index, item in enumerate(items):
        if position == len(indices):
            return
        if index == indices[position]:
            position += 1
            yield checked(item)


def json_object(
    blob: bytes,
    indices: list[int] | None = None,
    extra: Iterable[tuple[str, list[int | None]]] = (),
) -> Iterator[str]:
    """A stored column set as JSON, each column kept at ``indices``, plus integer columns."""
    yield "{"
    written = 0
    for name in column_names(blob):
        yield ("," if written else "") + f'"{name}":'
        yield from json_array(selected(tokens(blob, name), indices))
        written += 1
    for name, column in extra:
        yield ("," if written else "") + f'"{name}":'
        yield from json_array("null" if item is None else str(item) for item in column)
        written += 1
    yield "}"
