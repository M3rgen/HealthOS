"""The operator's basemap for the workout map, from the same origin as the interface.

One file, chosen by ``MAP_TILES_FILE`` and never by the request, so no request can reach another
path. The browser reads it with HTTP range requests (PMTiles): a single range answers 206, several
ranges or one past the end 416. The file streams in chunks; it is never read into memory.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from app.api.deps import settings as settings_dep
from app.core.config import Settings

router = APIRouter(prefix="/api/map", tags=["map"])
TILES_NAME = "tiles.pmtiles"
# Behind the login like every /api/ route, so private; revalidated with the ETag after an hour,
# because the operator may replace the file (docs/reference/maps.md).
CACHE_CONTROL = "private, max-age=3600"
MISSING = "Harita altlığı kurulu değil / No basemap installed"


def tiles_stat(path: Path | None) -> os.stat_result | None:
    """The regular file the setting names, or None (unset, missing, a directory or a device)."""
    if path is None:
        return None
    try:
        result = path.expanduser().stat()
    except OSError:
        return None
    return result if stat.S_ISREG(result.st_mode) else None


@router.get(
    f"/{TILES_NAME}",
    response_class=FileResponse,
    responses={
        206: {"description": "One byte range"},
        404: {"description": MISSING},
        416: {"description": "Several ranges, or a range past the end"},
    },
)
async def map_tiles(request: Request, settings: Settings = Depends(settings_dep)) -> FileResponse:
    path = settings.map_tiles_file
    found = tiles_stat(path)
    if path is None or found is None:
        raise HTTPException(404, MISSING, headers={"Cache-Control": "no-store"})
    if "," in request.headers.get("range", ""):
        # PMTiles asks for one range at a time; a multipart answer is never needed.
        raise HTTPException(
            416,
            "Tek aralık / Single range only",
            headers={"Content-Range": f"bytes */{found.st_size}", "Cache-Control": "no-store"},
        )
    return FileResponse(
        path.expanduser(),
        stat_result=found,
        media_type="application/vnd.pmtiles",
        headers={"Cache-Control": CACHE_CONTROL},
    )
