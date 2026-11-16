"""Workout map basemap: login, one byte range at a time, no other path, 404 when absent.

The file is invented bytes in a temporary directory; no tiles or places are involved.
"""

import httpx
import pytest
from support.auth import BASE, build

from app.core.auth import COOKIE_NAME

URL = "/api/map/tiles.pmtiles"
CONTENT = bytes(range(256)) * 300  # 76 800 bytes, every offset recognisable
MISSING = {"detail": "Harita altlığı kurulu değil / No basemap installed"}


@pytest.fixture
def tiles(tmp_path):
    path = tmp_path / "map" / "region.pmtiles"
    path.parent.mkdir()
    path.write_bytes(CONTENT)
    (tmp_path / "map" / "secret.txt").write_bytes(b"next to the tiles")
    return path


def client(monkeypatch, tiles_file, authenticated=True):
    application = build(monkeypatch, map_tiles_file=tiles_file)
    cookies = {COOKIE_NAME: application.state.ui_sessions.issue()} if authenticated else {}
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(application), base_url=BASE, cookies=cookies
    )


async def test_login_is_required_and_nothing_is_cached_without_it(monkeypatch, tiles):
    async with client(monkeypatch, tiles, authenticated=False) as http:
        response = await http.get(URL, headers={"Range": "bytes=0-15"})
    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"
    assert response.content.find(CONTENT[:16]) == -1


@pytest.mark.parametrize(
    ("asked", "start", "end"),
    [("bytes=0-16383", 0, 16383), ("bytes=70000-", 70000, 76799), ("bytes=-100", 76700, 76799)],
)
async def test_one_range_answers_206_privately_cacheable(monkeypatch, tiles, asked, start, end):
    async with client(monkeypatch, tiles) as http:
        response = await http.get(URL, headers={"Range": asked})
    assert response.status_code == 206
    assert response.content == CONTENT[start : end + 1]
    assert response.headers["content-range"] == f"bytes {start}-{end}/{len(CONTENT)}"
    assert response.headers["content-type"] == "application/vnd.pmtiles"
    assert response.headers["cache-control"] == "private, max-age=3600"
    assert response.headers["accept-ranges"] == "bytes"
    assert not response.headers["etag"].startswith("W/")  # PMTiles ignores weak ETags


async def test_whole_file_without_a_range(monkeypatch, tiles):
    async with client(monkeypatch, tiles) as http:
        response = await http.get(URL)
    assert response.status_code == 200 and response.content == CONTENT
    assert response.headers["content-length"] == str(len(CONTENT))


@pytest.mark.parametrize("asked", ["bytes=0-9,20-29", "bytes=0-9, 5-15", f"bytes={len(CONTENT)}-"])
async def test_several_ranges_or_one_past_the_end_answer_416(monkeypatch, tiles, asked):
    async with client(monkeypatch, tiles) as http:
        response = await http.get(URL, headers={"Range": asked})
    assert response.status_code == 416
    assert response.headers["content-range"] == f"bytes */{len(CONTENT)}"
    assert CONTENT[:10] not in response.content


@pytest.mark.parametrize("setting", [None, "missing.pmtiles", "."])
async def test_absent_file_is_404_so_the_interface_falls_back(monkeypatch, tiles, setting):
    tiles_file = None if setting is None else tiles.parent / setting
    async with client(monkeypatch, tiles_file) as http:
        response = await http.get(URL, headers={"Range": "bytes=0-16383"})
    assert response.status_code == 404 and response.json() == MISSING
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "path",
    [
        "/api/map/secret.txt",
        "/api/map/region.pmtiles",
        "/api/map/%2e%2e%2fsecret.txt",
        "/api/map/tiles.pmtiles%2f..%2fsecret.txt",
        "/api/map/tiles.pmtiles/../secret.txt",
        "/api/map/..%5csecret.txt",
        "/api/map/",
    ],
)
async def test_no_request_reaches_another_path(monkeypatch, tiles, path):
    async with client(monkeypatch, tiles) as http:
        response = await http.get(path)
    assert response.status_code == 404
    assert b"next to the tiles" not in response.content and CONTENT[:64] not in response.content
