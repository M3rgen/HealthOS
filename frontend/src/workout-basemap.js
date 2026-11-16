// Whether the map can show a basemap, decided before MapLibre is downloaded: WebGL2, then the first
// 127 bytes of the operator's PMTiles file (its header names the area it covers).

export const TILES_PATH = '/api/map/tiles.pmtiles';
const HEADER_BYTES = 127;

/** Can this browser draw MapLibre at all? The probe context is released at once. */
export function hasWebgl2() {
  try {
    const context = document.createElement('canvas').getContext('webgl2');
    context?.getExtension('WEBGL_lose_context')?.loseContext();
    return Boolean(context);
  } catch {
    return false;
  }
}

/**
 * The area a PMTiles v3 header covers, as [south, west, north, east] degrees, or null when the
 * bytes are not such a header.
 * @param {ArrayBuffer} bytes
 */
export function headerBounds(bytes) {
  if (bytes.byteLength < HEADER_BYTES) return null;
  const view = new DataView(bytes);
  const magic = String.fromCharCode(...new Uint8Array(bytes, 0, 7));
  if (magic !== 'PMTiles' || view.getUint8(7) !== 3) return null;
  const e7 = (/** @type {number} */ offset) => view.getInt32(offset, true) / 1e7;
  const [west, south, east, north] = [102, 106, 110, 114].map(e7);
  return west < east && south < north ? [south, west, north, east] : null;
}

/**
 * 'ready' with the covered area, or why there is no basemap: 'noTiles' (the server has none) or
 * 'mapFailed' (anything else). A missing file is the normal case, not an error.
 * @param {string} url @returns {Promise<{state: 'ready', bounds: number[]} | {state: 'noTiles' | 'mapFailed'}>}
 */
export async function probeBasemap(url) {
  try {
    const response = await fetch(url, { headers: { Range: `bytes=0-${HEADER_BYTES - 1}` } });
    if (response.status === 404) return { state: 'noTiles' };
    if (!response.ok) return { state: 'mapFailed' };
    const bounds = headerBounds(await response.arrayBuffer());
    return bounds ? { state: 'ready', bounds } : { state: 'mapFailed' };
  } catch {
    return { state: 'mapFailed' };
  }
}
