"""Location checks for the public snapshot: no coordinate may hide in a text file.

A workout track is the most sensitive record HealthOS keeps. Data files are rejected by
extension and signature (github_snapshot_checks.py); this module finds coordinates pasted into
text: decimal latitude/longitude pairs, FIT semicircle and E7 integers, GeoJSON or KML
`coordinates`, and encoded polylines. Findings name a path, a line and a kind, never a value.
"""

from __future__ import annotations

import bisect
import re
from collections.abc import Iterator

# Invented tracks (tests/fit_factory.py) circle 0.005 degrees around 0° N 0° E: open water in the
# Gulf of Guinea, more than 500 km from any coast. Coordinates inside this box are synthetic by
# construction and may be published; every other position stops the snapshot.
# Minimum latitude, minimum longitude, maximum latitude, maximum longitude (degrees).
FICTIONAL_REGION = (-1.0, -1.0, 1.0, 1.0)

# Decimal places a value needs to count as a position: an unlabelled pair needs four (about
# 11 m); a value named latitude/longitude or listed under `coordinates` needs three (about 110 m).
BARE_DECIMALS = 4
NAMED_DECIMALS = 3

# Reviewed exceptions: path -> (SHA-256 of the blob, what the content is, why it may be public).
# Empty on purpose: rewrite a false positive (or keep it inside FICTIONAL_REGION) before adding one.
LOCATION_ALLOWLIST: dict[bytes, tuple[str, str, str]] = {}

SEMICIRCLE = 180 / 2**31
LATITUDE_WORDS = frozenset({"lat", "latitude", "enlem"})
LONGITUDE_WORDS = frozenset({"lon", "lng", "long", "longitude", "boylam"})
PAIR_WINDOW = 200  # characters between a named latitude and its longitude
CONTEXT_WINDOW = 300  # characters around a `position_lat/long` name for integer pairs
COORDINATE_SPAN = 4096  # characters read after a `coordinates` key
RUN_WINDOW = 240  # characters searched on each side of a pair for a number series

NUMBER = r"-?\d+(?:\.\d+)?"
_NAMED = re.compile(
    rf"(?<!\w)(?P<key>[A-Za-z_][A-Za-z0-9_]*+)[\s\"':=,|>]{{1,6}}(?P<value>{NUMBER})(?![\w.])"
)
# An optional degree sign and hemisphere letter: `0.4827° S, 0.3914° W`.
_DEGREE = r"(?:[ ]?°)?(?:[ ]?[NSEW](?!\w))?"
_BARE_PAIR = re.compile(
    rf"(?<![\w.])(-?\d{{1,3}}\.\d+){_DEGREE}[ \t]*[,;\t][ \t]*(-?\d{{1,3}}\.\d+){_DEGREE}(?![\w.])"
)
_RUN_BEFORE = re.compile(rf"(?:{NUMBER}[ \t]*[,;\t][ \t]*)+$")
_RUN_AFTER = re.compile(rf"(?:[ \t]*[,;\t][ \t]*{NUMBER})+")
_INT_PAIR = re.compile(r"(?<![\w.])(-?\d{1,10})[\s,;]{1,4}(-?\d{1,10})(?![\w.])")
_POSITION_NAME = re.compile(r"position_?(?:lat|long)", re.IGNORECASE)
_COORDINATES = re.compile(r"[\"']?coordinates[\"']?\s*[:=]\s*\[|<coordinates>", re.IGNORECASE)
_COORDINATE_TUPLE = re.compile(rf"(?<![\w.])({NUMBER})\s*,\s*({NUMBER})(?![\w.])")
_SVG_GEOMETRY = re.compile(
    r"""\b(?:d|points|viewBox|transform)\s*=\s*["']|<(?:path|polyline|polygon)\b|\bmatrix\(""",
    re.IGNORECASE,
)
_POLYLINE_NAMED = re.compile(
    r"""(?<!\w)(?P<key>[A-Za-z_]\w*+)["']?\s*[:=]\s*(?P<quote>["'])(?P<text>[?-~]{8,}?)(?P=quote)"""
)
_POLYLINE_LITERAL = re.compile(r"""(?<=["'])[?-~]{16,}(?=["'])""")
# Polyline characters that identifiers and words never contain; an unnamed literal needs one.
_POLYLINE_MARKS = frozenset("?@\\^`{|}~")
_POLYLINE_KEY_WORDS = frozenset({"polyline", "encoded", "points"})


def real_position(latitude: float, longitude: float) -> bool:
    """A valid position outside the fictional region."""
    if not (abs(latitude) <= 90 and abs(longitude) <= 180):
        return False
    south, west, north, east = FICTIONAL_REGION
    return not (south <= latitude <= north and west <= longitude <= east)


def decimals(token: str) -> int:
    return len(token.partition(".")[2])


def key_words(key: str) -> set[str]:
    """`startLatitude` -> {start, latitude}; `position_long` -> {position, long}."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key)
    return {word.lower() for word in re.split(r"[_\d]+", spaced) if word}


def named_scale(key: str, value: str) -> tuple[str, float] | None:
    """The axis and degree scale of a named value, or None when the name is not a position."""
    words = key_words(key)
    axis = "lat" if words & LATITUDE_WORDS else "lon" if words & LONGITUDE_WORDS else None
    if axis is None or (words & LATITUDE_WORDS and words & LONGITUDE_WORDS):
        return None
    if "." in value:
        return (axis, 1.0) if decimals(value) >= NAMED_DECIMALS else None
    if key.lower().endswith("e7"):
        return axis, 1e-7
    if words & {"position", "semicircle", "semicircles"}:
        return axis, SEMICIRCLE
    return None


def named_pairs(text: str) -> Iterator[int]:
    """Positions where a named latitude and a named longitude sit close together."""
    found: dict[str, list[tuple[int, float]]] = {"lat": [], "lon": []}
    for match in _NAMED.finditer(text):
        scale = named_scale(match["key"], match["value"])
        if scale is not None:
            found[scale[0]].append((match.start(), float(match["value"]) * scale[1]))
    longitudes = found["lon"]  # in text order
    offsets = [offset for offset, _ in longitudes]
    for start, latitude in found["lat"]:
        low = bisect.bisect_left(offsets, start - PAIR_WINDOW)
        high = bisect.bisect_right(offsets, start + PAIR_WINDOW)
        if any(real_position(latitude, longitude) for _, longitude in longitudes[low:high]):
            yield start


def near_whole_degrees(*values: float) -> bool:
    """Boundary test constants such as 30.000001 or 89.999999, not measured positions."""
    return all(abs(value - round(value)) < 1e-4 for value in values)


def precise_neighbours(text: str, start: int, end: int) -> int:
    """High-precision numbers joined to the pair by separators on the same line (a series)."""
    line_start = text.rfind("\n", 0, start) + 1
    before = _RUN_BEFORE.search(text[max(line_start, start - RUN_WINDOW) : start])
    after = _RUN_AFTER.match(text[end : end + RUN_WINDOW])
    run = (before.group() if before else "") + (after.group() if after else "")
    return sum(decimals(number) >= BARE_DECIMALS for number in re.findall(NUMBER, run))


def bare_pairs(text: str) -> Iterator[int]:
    """Unlabelled `a, b` pairs; a CSV row may carry one more precise value, a series may not."""
    svg_lines: dict[int, bool] = {}
    for match in _BARE_PAIR.finditer(text):
        first, second = match[1], match[2]
        if min(decimals(first), decimals(second)) < BARE_DECIMALS:
            continue
        a, b = float(first), float(second)
        if near_whole_degrees(a, b) or not (real_position(a, b) or real_position(b, a)):
            continue
        if precise_neighbours(text, match.start(), match.end()) > 1:
            continue
        line_start = text.rfind("\n", 0, match.start()) + 1
        if line_start not in svg_lines:
            line_end = text.find("\n", match.end())
            line = text[line_start : None if line_end < 0 else line_end]
            svg_lines[line_start] = _SVG_GEOMETRY.search(line) is not None
        if not svg_lines[line_start]:
            yield match.start()


def semicircle_pairs(text: str) -> Iterator[int]:
    """Adjacent integer pairs read as FIT semicircles next to a `position_lat/long` name."""
    for match in _INT_PAIR.finditer(text):
        latitude, longitude = (int(value) * SEMICIRCLE for value in match.groups())
        if real_position(latitude, longitude) and _POSITION_NAME.search(
            text, max(0, match.start() - CONTEXT_WINDOW), match.end() + CONTEXT_WINDOW
        ):
            yield match.start()


def coordinate_span(text: str, start: int, kml: bool) -> str:
    """The text of one `coordinates` value: up to `</coordinates>` or the matching `]`."""
    span = text[start : start + COORDINATE_SPAN]
    if kml:
        closing = span.find("</coordinates>")
        return span if closing < 0 else span[:closing]
    depth = 1
    for index, character in enumerate(span):
        depth += {"[": 1, "]": -1}.get(character, 0)
        if depth == 0:
            return span[:index]
    return span


def geojson_coordinates(text: str) -> Iterator[int]:
    """`coordinates` arrays (GeoJSON, longitude first) and KML `<coordinates>` tuples."""
    for match in _COORDINATES.finditer(text):
        span = coordinate_span(text, match.end(), match.group().startswith("<"))
        for pair in _COORDINATE_TUPLE.finditer(span):
            longitude, latitude = pair[1], pair[2]
            if min(decimals(longitude), decimals(latitude)) >= NAMED_DECIMALS and real_position(
                float(latitude), float(longitude)
            ):
                yield match.start()
                break


def decode_polyline(encoded: str, precision: int) -> list[tuple[float, float]] | None:
    """Google's encoded polyline algorithm; None unless the whole text decodes to points."""
    values: list[int] = []
    shift = result = 0
    for character in encoded:
        chunk = ord(character) - 63
        if not 0 <= chunk < 64 or shift > 30:
            return None
        result |= (chunk & 0x1F) << shift
        shift += 5
        if chunk < 0x20:
            values.append(~(result >> 1) if result & 1 else result >> 1)
            shift = result = 0
    if shift or len(values) < 4 or len(values) % 2:
        return None
    points, latitude, longitude = [], 0, 0
    for step_latitude, step_longitude in zip(values[::2], values[1::2], strict=True):
        latitude += step_latitude
        longitude += step_longitude
        points.append((latitude / 10**precision, longitude / 10**precision))
    return points


def polyline_leaks(encoded: str, minimum_points: int) -> bool:
    """True when the text is a polyline (precision 5 or 6) with a real position."""
    for precision in (5, 6):
        points = decode_polyline(encoded.replace("\\\\", "\\"), precision)
        if (
            points
            and len(points) >= minimum_points
            and all(abs(lat) <= 90 and abs(lon) <= 180 for lat, lon in points)
            and any(real_position(lat, lon) for lat, lon in points)
        ):
            return True
    return False


def polylines(text: str) -> Iterator[int]:
    """Polyline strings: any length under a polyline/points name; else quoted literals of at
    least 16 characters with a character no identifier has."""
    for match in _POLYLINE_NAMED.finditer(text):
        if key_words(match["key"]) & _POLYLINE_KEY_WORDS and polyline_leaks(match["text"], 2):
            yield match.start()
    for match in _POLYLINE_LITERAL.finditer(text):
        if _POLYLINE_MARKS.intersection(match.group()) and polyline_leaks(match.group(), 4):
            yield match.start()


DETECTORS = (
    ("named latitude/longitude", named_pairs),
    ("decimal coordinate pair", bare_pairs),
    ("semicircle pair near position_lat/long", semicircle_pairs),
    ("GeoJSON/KML coordinates", geojson_coordinates),
    ("encoded polyline", polylines),
)


def find_locations(data: bytes) -> list[tuple[int, str]]:
    """(line, kind) of every suspected real position in a text blob; binaries are skipped."""
    if b"\0" in data:
        return []
    text = data.decode("utf-8", errors="replace")
    findings = set()
    for kind, detector in DETECTORS:
        for offset in detector(text):
            findings.add((text.count("\n", 0, offset) + 1, kind))
    return sorted(findings)
