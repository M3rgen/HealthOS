# Workout map

**Spor** opens a workout that has a recorded track (`has_track`, see
[Garmin FIT tracks](garmin-tracks.md#track-api)) in a detail view: the route on a map, charts of
speed or pace, heart rate and elevation (cadence, power and temperature when the file has them),
summary cards and the lap table. The charts and the route share one cursor: hovering or tapping
a chart marks that moment on the route, tapping the route moves the charts there, and the arrow
keys walk the chart stack. The horizontal axis is time or distance; runs and walks show pace
(min/km) first. Timer pauses are shaded on the time axis and marked on the distance axis.

The view, its charts and its styles load when a workout is first opened; the map library only
when a basemap can actually be shown. Nothing in it is part of the first page load
([performance budget](../PERFORMANCE_BUDGET.md)).

## Basemap

The map draws on vector tiles from **your own** [PMTiles](https://docs.protomaps.com/pmtiles/)
file, served by HealthOS itself from `GET /api/map/tiles.pmtiles` (behind the login, HTTP range
requests). The browser never contacts another server: the page's Content Security Policy stays
`'self'`, and the MapLibre worker is a file of the same build (`worker-src 'self'`, already in the
policy). Set `MAP_TILES_FILE` to the file ([configuration](config.md)); restart the app after
setting it for the first time. The file can be replaced later without a restart.

Without the file, when the route lies outside the area it covers, or in a browser without WebGL2,
the view draws the route on its own (start and finish, lap boundaries, a scale bar) with a one-line
note. A workout without positions (an indoor session) shows the charts only. The view is never
empty.

### Cut your region

The [Protomaps basemap](https://docs.protomaps.com/basemaps/downloads) is built daily from
OpenStreetMap (schema version 4; the style here reads its `earth`, `water`, `landcover`,
`landuse`, `roads` and `buildings` layers). Extract only the area you train in with the
[`pmtiles` command](https://github.com/protomaps/go-pmtiles/releases) (a single binary):

```sh
# Pick a build from https://maps.protomaps.com/builds/ (one file per day, YYYYMMDD).
pmtiles extract https://build.protomaps.com/<YYYYMMDD>.pmtiles tiles.pmtiles \
  --bbox=<west>,<south>,<east>,<north> --maxzoom=14 --dry-run   # prints the size only
pmtiles extract https://build.protomaps.com/<YYYYMMDD>.pmtiles tiles.pmtiles \
  --bbox=<west>,<south>,<east>,<north> --maxzoom=14
```

The extract reads only the byte ranges of your area; the planet file is not downloaded. Keep the
bounding box a little larger than where you train: a route that leaves it is drawn without a
basemap. Each extra zoom level roughly doubles the size. Measured for an area of about 4° × 2°
(a small country with its border strips) on 15 November 2026:

| `--maxzoom` | Size |
| --- | --- |
| 10 | 26 MB |
| 12 | 142 MB |
| 13 | 287 MB |
| 14 | about 570 MB |
| 15 | 1.3 GB |

Zoom 14 is enough for streets and footpaths; the map enlarges zoom-14 tiles beyond that. Refresh
the file every few months if you want newer streets; the builds are not meant for hotlinking, so
always extract a copy.

### Install the file

Copy the file to the server, readable by the app's user, and point `MAP_TILES_FILE` at it. With
Compose, use the overlay [`docker-compose.map.yml`](../../docker-compose.map.yml) without editing
tracked files:

```sh
sudo install -d -m 755 /opt/healthos/map
sudo install -m 644 tiles.pmtiles /opt/healthos/map/tiles.pmtiles
# .env (join overlays with ":", e.g. after docker-compose.garmin.yml):
#   COMPOSE_FILE=docker-compose.yml:docker-compose.map.yml
#   MAP_TILES_HOST_DIR=/opt/healthos/map
docker compose up -d app
# The token goes through stdin, never the command line:
printf 'header = "Authorization: Bearer %s"\n' "$APP_UI_TOKEN" | curl --config - -s -o /dev/null \
  -w '%{http_code}\n' -H 'Range: bytes=0-126' http://127.0.0.1:8000/api/map/tiles.pmtiles   # 206
```

The overlay mounts the directory, not the file, so replacing `tiles.pmtiles` with `mv` reaches the
running container. Browsers keep tiles for an hour (`Cache-Control: private, max-age=3600`) and
revalidate with the file's `ETag`.

### The endpoint

`GET /api/map/tiles.pmtiles` serves exactly the file `MAP_TILES_FILE` names; the request never
chooses a path. One byte range answers `206` with `Content-Range`, a request without `Range` the
whole file, both streamed in chunks (the file is never read into memory). Several ranges in one
request, or a range that starts past the end, answer `416`. An unset setting, a missing file or a
path that is not a regular file answer `404` (`no-store`), and the interface falls back to the
route without a basemap. Like every `/api/` route it needs the login session or the bearer token.

## Style

The style is plain and dark, close to the interface's own surfaces: land, water, green areas,
roads by class, footpaths dashed and buildings from zoom 14, with the route as the brightest line.
It has **no labels or icons**, so it needs no glyph (font) or sprite files: nothing beyond the one
PMTiles file has to be installed. Street and place names would need either glyph files served from
this origin or MapLibre's `font-faces` with a self-hosted font; that is a later choice.

## Attribution and licences

- Map data © [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors, under the
  ODbL. Protomaps basemap builds are ODbL produced works of OpenStreetMap: they require the
  OpenStreetMap credit, and Protomaps asks to be credited beside it. Whenever a basemap is drawn,
  the map's corner shows "Protomaps © OpenStreetMap contributors", each name linking to
  [protomaps.com](https://protomaps.com) and the OpenStreetMap copyright page; the credit stays
  visible and is not collapsed. The route-only drawing uses no map data and shows none. If you
  build the file from another OpenStreetMap-based source, its licence may ask for more.
- [MapLibre GL JS](https://github.com/maplibre/maplibre-gl-js) and
  [PMTiles](https://github.com/protomaps/PMTiles) are BSD-3-Clause. Their licence texts, with
  those of every other library in the interface build, are in
  [`THIRD_PARTY_LICENSES.md`](../../THIRD_PARTY_LICENSES.md), which also ships with the build.

## Privacy

The track and the basemap stay on your server and in your browser: the route is drawn from
`GET /api/dashboard/workouts/{workout_id}/track`, the tiles from your own file, and no tile, font
or script is fetched from elsewhere. The repository holds no map file and no real coordinates;
previews and tests use an invented track and an invented basemap around 0° N 0° E
(`frontend/preview/track-fixture.cjs`, `frontend/preview/synthetic-tiles.cjs`).
