// The workout map: MapLibre GL draws the operator's own PMTiles basemap, served from this origin
// (GET /api/map/tiles.pmtiles), with the route on top. Loaded only when a track with positions is
// opened, the basemap file exists and covers the route, and the browser has WebGL2; anything that
// fails here rejects, and the view draws the route without a basemap instead.
import { Map as MapLibre, addProtocol, setWorkerUrl } from 'maplibre-gl';
import workerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?url';
import { PMTiles, Protocol } from 'pmtiles';
import { mapStyle } from './workout-map-style.js';
import { lapBoundaries, nearestPoint, routeEnds, routeRuns } from './workout-track.js';

const REACH = 28;
/** @type {Protocol | undefined} */
let protocol;

/** The route, its ends and lap boundaries as GeoJSON. @param {import('./workout-track.js').Track} track */
function routeData(track) {
  const { latitude = [], longitude = [] } = track.samples;
  const at = (/** @type {number} */ i) => /** @type {[number, number]} */ ([longitude[i], latitude[i]]);
  const ends = /** @type {{first: number, last: number}} */ (routeEnds(track.samples));
  /** @type {import('geojson').Feature[]} */
  const marks = [['start', ends.first], ['end', ends.last], ...lapBoundaries(track.laps).map(i => ['lap', i])]
    .filter(([, i]) => latitude[Number(i)] != null && longitude[Number(i)] != null)
    .map(([kind, i]) => ({ type: 'Feature', properties: { kind }, geometry: { type: 'Point', coordinates: at(Number(i)) } }));
  /** @type {import('geojson').Feature} */
  const route = { type: 'Feature', properties: {}, geometry: { type: 'MultiLineString', coordinates: routeRuns(track.samples).map(run => run.points) } };
  return { route, marks: { type: 'FeatureCollection', features: marks } };
}

// A map that has not drawn its style by then (a blocked worker, a stalled file) gives way to the
// route drawn without a basemap; the view never waits on it.
const LOAD_TIMEOUT_MS = 20000;

/** @param {MapLibre} map @returns {Promise<void>} */
function loaded(map) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('map load timed out')), LOAD_TIMEOUT_MS);
    map.once('load', () => { clearTimeout(timer); resolve(); });
    map.once('error', event => { clearTimeout(timer); reject(event.error ?? new Error('map error')); });
  });
}

/** @param {MapLibre} map @param {string} id @param {import('geojson').GeoJSON} data */
function setData(map, id, data) {
  /** @type {import('maplibre-gl').GeoJSONSource | undefined} */ (map.getSource(id))?.setData(data);
}

/**
 * Opens the map in `host`. Resolves with the controls the view needs once the style has loaded;
 * rejects when MapLibre cannot start or the basemap fails before that.
 * @param {HTMLElement} host @param {import('./workout-track.js').Track} track @param {string} url same-origin PMTiles URL
 * @param {(index: number) => void} onPick @param {string} label the canvas's accessible name
 */
export async function openMap(host, track, url, onPick, label) {
  if (!protocol) {
    setWorkerUrl(workerUrl);
    protocol = new Protocol();
    addProtocol('pmtiles', protocol.tile);
  }
  protocol.add(new PMTiles(url));
  const [south, west, north, east] = /** @type {number[]} */ (track.summary.bounds);
  const map = new MapLibre({
    container: host, style: mapStyle(`pmtiles://${url}`), bounds: [west, south, east, north],
    fitBoundsOptions: { padding: 36 }, attributionControl: false, dragRotate: false, pitchWithRotate: false,
    touchPitch: false, maxPitch: 0, fadeDuration: 0, locale: { 'Map.Title': label },
  });
  map.touchZoomRotate.disableRotation();
  map.keyboard.disableRotation();
  try {
    await loaded(map);
  } catch (error) {
    map.remove();
    throw error;
  }
  const data = routeData(track);
  setData(map, 'route', data.route);
  setData(map, 'marks', /** @type {import('geojson').FeatureCollection} */ (data.marks));
  map.on('click', event => {
    const { latitude = [], longitude = [] } = track.samples;
    const points = latitude.map((lat, i) => {
      const lon = longitude[i];
      if (lat == null || lon == null) return null;
      const point = map.project([lon, lat]);
      return /** @type {[number, number]} */ ([point.x, point.y]);
    });
    const found = nearestPoint(points, event.point.x, event.point.y, REACH);
    if (found >= 0) onPick(found);
  });
  return {
    /** @param {number} index */
    setCursor(index) {
      const { latitude = [], longitude = [] } = track.samples;
      const lat = latitude[index], lon = longitude[index];
      setData(map, 'cursor', { type: 'FeatureCollection', features: lat == null || lon == null ? [] :
        [{ type: 'Feature', properties: {}, geometry: { type: 'Point', coordinates: [lon, lat] } }] });
    },
    fit: () => map.fitBounds([west, south, east, north], { padding: 36 }),
    zoomIn: () => map.zoomIn(),
    zoomOut: () => map.zoomOut(),
    /** Resolves once every visible tile has been drawn. */
    idle: () => new Promise(resolve => { if (map.loaded() && !map.isMoving()) resolve(undefined); else map.once('idle', () => resolve(undefined)); }),
    destroy: () => map.remove(),
  };
}
