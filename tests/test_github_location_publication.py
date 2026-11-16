"""Coordinates must not reach a public snapshot as text; invented ones inside the box may.

Every position that should be caught is computed at run time from integer parts, so this file
itself carries no coordinate and passes the gate it tests. Both places are open water far from
any coast: the Southern Ocean between New Zealand and Antarctica, and the central Arctic Ocean.
"""

import hashlib
import json

import pytest
from test_branch_scripts import ROOT, Repo
from test_branch_scripts import repo as repo

from scripts import github_location_checks as locations
from scripts import github_snapshot_checks as checks

SOUTH = (-(61 + 482_731 / 10**6), -(152 + 391_407 / 10**6))
NORTH = (84 + 317_206 / 10**6, 137 + 540_812 / 10**6)
INVENTED = (0.004_321, -0.003_456)  # inside FICTIONAL_REGION, like tests/fit_factory.py


def degrees(value: float, places: int = 6) -> str:
    return f"{value:.{places}f}"


def semicircles(value: float) -> int:
    return round(value * 2**31 / 180)


def track(start: tuple[float, float], count: int = 6) -> list[tuple[float, float]]:
    return [(start[0] + index * 1e-4, start[1] + index * 2e-4) for index in range(count)]


def encode_polyline(points: list[tuple[float, float]], precision: int = 5) -> str:
    """Google's encoded polyline algorithm, the inverse of decode_polyline."""
    characters, previous = [], (0, 0)
    for point in points:
        current = (round(point[0] * 10**precision), round(point[1] * 10**precision))
        for delta in (current[0] - previous[0], current[1] - previous[1]):
            value = ~(delta << 1) if delta < 0 else delta << 1
            while value >= 0x20:
                characters.append(chr((0x20 | (value & 0x1F)) + 63))
                value >>= 5
            characters.append(chr(value + 63))
        previous = current
    return "".join(characters)


def lat_lon(place: tuple[float, float], places: int = 6) -> tuple[str, str]:
    return degrees(place[0], places), degrees(place[1], places)


LAT, LON = lat_lon(SOUTH)
NLAT, NLON = lat_lon(NORTH)
NAMED = "named latitude/longitude"
BARE = "decimal coordinate pair"
SEMICIRCLE = "semicircle pair near position_lat/long"
GEOJSON = "GeoJSON/KML coordinates"
POLYLINE = "encoded polyline"

CAUGHT = [
    ("json-keys", f'{{"lat": {LAT}, "lon": {LON}}}', NAMED),
    ("garmin-camel-case", f'{{"startLatitude": {NLAT},\n "startLongitude": {NLON}}}', NAMED),
    ("gpx-attributes", f'<trkpt lat="{LAT}" lon="{LON}"><ele>4.2</ele></trkpt>', NAMED),
    (
        "tcx-elements",
        f"<LatitudeDegrees>{NLAT}</LatitudeDegrees><LongitudeDegrees>{NLON}</LongitudeDegrees>",
        NAMED,
    ),
    ("python-keywords-three-places", f"Point(lat={LAT[:-3]}, lng={LON[:-3]})", NAMED),
    (
        "e7-integers",
        f'"latitudeE7": {round(SOUTH[0] * 1e7)}, "longitudeE7": {round(SOUTH[1] * 1e7)}',
        NAMED,
    ),
    (
        "semicircle-fields",
        f"position_lat={semicircles(NORTH[0])} position_long={semicircles(NORTH[1])}",
        NAMED,
    ),
    (
        "semicircle-table",
        f"record: position_lat position_long\n{semicircles(SOUTH[0])}, {semicircles(SOUTH[1])}",
        SEMICIRCLE,
    ),
    ("bare-pair", f"Buluşma: {LAT}, {LON}\n", BARE),
    ("bare-pair-lon-first", f"[{NLON},{NLAT}]", BARE),
    ("csv-row-with-altitude", f"time,a,b,c\n12:00:01,{LAT},{LON},4.5\n", BARE),
    ("semicolon-pair", f"{NLAT};{NLON}", BARE),
    ("degrees-hemispheres", f"{LAT[1:]}° S, {LON[1:]}° W", BARE),
    (
        "geojson-pretty",
        json.dumps(
            {"type": "LineString", "coordinates": [[p[1], p[0]] for p in track(SOUTH, 2)]}, indent=2
        ),
        GEOJSON,
    ),
    ("kml", f"<coordinates>{NLON},{NLAT},0 {LON},{LAT},0</coordinates>", GEOJSON),
    (
        "polyline-named",
        json.dumps({"summary_polyline": encode_polyline(track(SOUTH, 2))}),
        POLYLINE,
    ),
    ("polyline-points", f"overview: {{points: '{encode_polyline(track(NORTH, 2))}'}}", POLYLINE),
    ("polyline-literal", f'TRACK = "{encode_polyline(track(NORTH))}"\n', POLYLINE),
    ("polyline-precision-6", f"x = '{encode_polyline(track(SOUTH), 6)}'", POLYLINE),
]

ILAT, ILON = lat_lon(INVENTED)
NOT_CAUGHT = [
    ("versions", 'version = "0.2.3.1"\nPython 3.12.16, uv 0.13.6, node 22.11.0\n'),
    ("ip-shaped-and-single-decimals", "10.20.30.40 took 61.4827 s; pi 3.14159265\n"),
    ("time-zone-offsets", '"offset": "+05:30", UTC-09:30, 2026-11-15T07:00:00+03:00\n'),
    ("ratios-in-region", "ratio 0.1234, 0.5678; share 0.9876;0.1234\n"),
    ("three-places-bare", "12.345, 67.891\n"),
    ("space-separated", "12.3456 78.9012\n"),
    ("long-number-list", "[51.4375, 51.4375, 101.9375, 49.1234]\n"),
    ("svg-path", '<path d="M 12.3456,7.8901 L 23.4567,8.9012 Z"/>\n'),
    ("css-easing", "transition: cubic-bezier(0.1234, 0.5678, 0.9012, 0.3456)\n"),
    ("transform-matrix", '<g transform="matrix(1, 0, 0, 1, 12.3456, 45.6789)">\n'),
    ("whole-degree-boundaries", "[(30.000001, 89.999999)]\n"),
    ("out-of-range", '{"lat": 123.4567, "lon": 200.1234}\n'),
    ("not-coordinate-keys", '{"latency": 61.4827, "longest": 152.3914, "başlat": 12.3456}\n'),
    ("invented-named", f'{{"lat": {ILAT}, "lon": {ILON}}}\n{ILAT}, {ILON}\n'),
    (
        "invented-semicircles",
        f"position_lat={semicircles(INVENTED[0])} position_long={semicircles(INVENTED[1])}",
    ),
    ("invented-geojson", json.dumps({"coordinates": [[INVENTED[1], INVENTED[0]]]})),
    ("invented-polyline", f'"{encode_polyline(track(INVENTED))}"'),
    ("identifier-literal", '"endTimeGMT" "HEALTHOS_SOURCES" "rows[ACROSS_MIDNIGHT]"\n'),
    ("hash-and-base64", "sha256-3q2+7wAbCdEfGh1234567890== a1b2c3d4e5f6\n"),
    ("unix-timestamps", "position_lat updated 1763190000, 1763190060\n"),
]


@pytest.mark.parametrize("text,kind", [c[1:] for c in CAUGHT], ids=[c[0] for c in CAUGHT])
def test_real_positions_are_found(text: str, kind: str) -> None:
    assert kind in {found for _, found in locations.find_locations(text.encode())}


@pytest.mark.parametrize("text", [c[1] for c in NOT_CAUGHT], ids=[c[0] for c in NOT_CAUGHT])
def test_lookalikes_and_invented_positions_pass(text: str) -> None:
    assert locations.find_locations(text.encode()) == []


def test_findings_name_lines_and_binary_blobs_are_skipped() -> None:
    text = f"first\nsecond\nlat={LAT} lon={LON}\n"
    assert locations.find_locations(text.encode()) == [(3, NAMED)]
    assert locations.find_locations(b"\0" + text.encode()) == []


def test_region_box_covers_the_invented_track_generator() -> None:
    south, west, north, east = locations.FICTIONAL_REGION
    assert south < -0.005 and west < -0.005 and north > 0.005 and east > 0.005
    assert not locations.real_position(0.005, -0.005)
    assert locations.real_position(north + 0.01, 0) and locations.real_position(0, west - 0.01)


def blob_entries(repo: Repo) -> list[tuple[bytes, bytes, bytes]]:
    entries = []
    for line in repo.git("ls-tree", "-r", "HEAD").splitlines():
        metadata, path = line.split("\t", 1)
        entries.append((b"blob", metadata.split()[2].encode(), path.encode()))
    return entries


def test_dry_run_stops_without_printing_the_value(repo: Repo) -> None:
    path = "docs/route-notes.md"
    repo.commit("synthetic location fixture", {path: f"Start: {LAT}, {LON}\n"})
    before = repo.git("rev-parse", "github-main"), repo.git("write-tree")
    for arguments in (("--dry-run",), ("--dry-run", "--allow-references"), ()):
        result = repo.run("github_snapshot.sh", *arguments)
        assert result.returncode == 1, result.stdout + result.stderr
        assert f"location '{path}' line 1: {BARE}" in result.stderr
        assert LAT not in result.stdout + result.stderr
    assert before == (repo.git("rev-parse", "github-main"), repo.git("write-tree"))
    repo.commit("invented location", {path: f"Start: {ILAT}, {ILON}\n"})
    assert repo.run("github_snapshot.sh", "--dry-run").returncode == 0


def test_allowance_needs_the_exact_reviewed_bytes(
    repo: Repo, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    content = f"{{'lat': {NLAT}, 'lon': {NLON}}}\n".encode()
    repo.commit("synthetic location fixture", {"fixture.txt": content.decode()})
    monkeypatch.chdir(repo.path)
    with pytest.raises(SystemExit):
        checks.check_locations(blob_entries(repo))
    assert NLAT not in capsys.readouterr().err
    digest = hashlib.sha256(content).hexdigest()
    reviewed = {b"fixture.txt": (digest, "one synthetic pair", "test of the allowance")}
    monkeypatch.setattr(checks.locations, "LOCATION_ALLOWLIST", reviewed)
    checks.check_locations(blob_entries(repo))
    repo.commit("changed fixture", {"fixture.txt": content.decode() + "\n"})
    with pytest.raises(SystemExit):
        checks.check_locations(blob_entries(repo))


def test_current_tree_location_gate_without_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(ROOT)
    entries = []
    for entry in checks.git("ls-files", "--stage", "-z").split(b"\0"):
        if entry:
            metadata, path = entry.split(b"\t", 1)
            entries.append((b"blob", metadata.split()[1], path))
    checks.check_locations(entries)
    assert locations.LOCATION_ALLOWLIST == {}
