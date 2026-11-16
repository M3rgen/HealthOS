// SYNTHETIC basemap for previews and browser checks: a tiny PMTiles v3 archive of invented vector
// tiles around 0° N 0° E (the fictional region of the preview track), built in memory at run time.
// Its layers follow the Protomaps v4 names the map style reads (earth, water, landuse, roads,
// buildings), so the map's basemap path runs end to end without a real map file, and nothing in
// it is a real place. Nothing is written to disk.
const { zxyToTileId } = require('pmtiles');

const EXTENT = 4096;
const AREA = [-0.03, -0.03, 0.03, 0.03]; // west, south, east, north
const MAX_ZOOM = 14;

// --- geometry, in degrees -------------------------------------------------------------------
const ring = (cx, cy, rx, ry, n = 24) => Array.from({ length: n + 1 }, (_, i) =>
  [cx + rx * Math.cos((i % n) / n * 2 * Math.PI), cy + ry * Math.sin((i % n) / n * 2 * Math.PI)]);
const box = (west, south, east, north) => [[west, south], [east, south], [east, north], [west, north], [west, south]];

function features() {
  const out = [{ layer: 'earth', kind: 'earth', type: 3, rings: [box(-0.05, -0.05, 0.05, 0.05)] },
    { layer: 'water', kind: 'lake', type: 3, rings: [ring(0.006, -0.009, 0.0042, 0.0026)] },
    { layer: 'water', kind: 'river', type: 2, lines: [[[-0.03, 0.0105], [-0.01, 0.0122], [0.012, 0.0101], [0.03, 0.0127]]] },
    { layer: 'landuse', kind: 'park', type: 3, rings: [ring(-0.006, 0.004, 0.0035, 0.0028)] },
    { layer: 'landuse', kind: 'wood', type: 3, rings: [box(0.012, 0.001, 0.02, 0.008)] },
    { layer: 'roads', kind: 'highway', type: 2, lines: [[[-0.03, -0.021], [0.03, -0.016]]] }];
  for (let k = -7; k <= 7; k++) {
    const kind = k % 3 ? 'minor_road' : 'major_road';
    out.push({ layer: 'roads', kind, type: 2, lines: [[[-0.03, k * 0.004], [0.03, k * 0.004 + 0.0007]]] });
    out.push({ layer: 'roads', kind, type: 2, lines: [[[k * 0.005, -0.03], [k * 0.005 - 0.0006, 0.03]]] });
  }
  out.push({ layer: 'roads', kind: 'path', type: 2, lines: [ring(0.0021, 0.0034, 0.0074, 0.0091, 40)] });
  for (let x = -6; x <= 6; x++) for (let y = -6; y <= 6; y++) {
    if ((x * 7 + y * 3) % 4 === 0) continue;
    const west = x * 0.005 + 0.0008, south = y * 0.004 + 0.0008;
    out.push({ layer: 'buildings', kind: 'building', type: 3, minzoom: 14, rings: [box(west, south, west + 0.0011, south + 0.0009)] });
  }
  return out;
}

// --- Mapbox Vector Tile encoding -------------------------------------------------------------
const varint = (value, out) => { while (value > 0x7f) { out.push((value & 0x7f) | 0x80); value = Math.floor(value / 128); } out.push(value); };
const zigzag = value => (value << 1) ^ (value >> 31);
const field = (number, wire, out) => varint((number << 3) | wire, out);
function bytes(number, payload, out) { field(number, 2, out); varint(payload.length, out); out.push(...payload); }

function project([lon, lat], z, x, y) {
  const scale = 2 ** z;
  const px = ((lon + 180) / 360) * scale, rad = (lat * Math.PI) / 180;
  const py = ((1 - Math.log(Math.tan(rad) + 1 / Math.cos(rad)) / Math.PI) / 2) * scale;
  return [Math.round((px - x) * EXTENT), Math.round((py - y) * EXTENT)];
}

function geometry(feature, z, x, y) {
  const out = [];
  let cx = 0, cy = 0;
  for (const part of feature.type === 3 ? feature.rings : feature.lines) {
    // MVT: an outer ring is clockwise on screen (y down), the reverse of these rings in degrees.
    const points = (feature.type === 3 ? [...part].reverse() : part).map(point => project(point, z, x, y));
    const body = feature.type === 3 ? points.slice(0, -1) : points;
    body.forEach(([px, py], i) => {
      if (i === 0) out.push((1 << 3) | 1);
      else if (i === 1) out.push(((body.length - 1) << 3) | 2);
      out.push(zigzag(px - cx), zigzag(py - cy));
      cx = px; cy = py;
    });
    if (feature.type === 3) out.push((1 << 3) | 7);
  }
  return out;
}

function layer(name, items, z, x, y) {
  const kinds = [...new Set(items.map(item => item.kind))];
  const out = [];
  field(15, 0, out); varint(2, out);
  bytes(1, [...Buffer.from(name)], out);
  for (const item of items) {
    const feature = [], tags = [], packed = [];
    varint(0, tags); varint(kinds.indexOf(item.kind), tags);
    bytes(2, tags, feature);
    field(3, 0, feature); varint(item.type, feature);
    for (const value of geometry(item, z, x, y)) varint(value, packed);
    bytes(4, packed, feature);
    bytes(2, feature, out);
  }
  bytes(3, [...Buffer.from('kind')], out);
  for (const kind of kinds) { const value = []; bytes(1, [...Buffer.from(kind)], value); bytes(4, value, out); }
  field(5, 0, out); varint(EXTENT, out);
  return out;
}

function tile(z, x, y, all) {
  const out = [];
  const shown = all.filter(item => z >= (item.minzoom ?? 0));
  for (const name of [...new Set(shown.map(item => item.layer))]) bytes(3, layer(name, shown.filter(item => item.layer === name), z, x, y), out);
  return Buffer.from(out);
}

// --- PMTiles v3 ------------------------------------------------------------------------------
function tileRange(z) {
  const [west, south, east, north] = AREA, scale = 2 ** z;
  const column = lon => Math.floor(((lon + 180) / 360) * scale);
  const row = lat => { const rad = (lat * Math.PI) / 180; return Math.floor(((1 - Math.log(Math.tan(rad) + 1 / Math.cos(rad)) / Math.PI) / 2) * scale); };
  const out = [];
  for (let x = column(west); x <= column(east); x++) for (let y = row(north); y <= row(south); y++) out.push([z, x, y]);
  return out;
}

function directory(entries) {
  const out = [];
  varint(entries.length, out);
  let last = 0;
  for (const entry of entries) { varint(entry.id - last, out); last = entry.id; }
  entries.forEach(() => varint(1, out)); // run length: one tile each
  for (const entry of entries) varint(entry.length, out);
  entries.forEach((entry, i) => varint(i && entry.offset === entries[i - 1].offset + entries[i - 1].length ? 0 : entry.offset + 1, out));
  return Buffer.from(out);
}

function header(sizes) {
  const out = Buffer.alloc(127);
  out.write('PMTiles', 0, 'latin1');
  out.writeUInt8(3, 7);
  const numbers = [127, sizes.root, 127 + sizes.root, sizes.metadata, 127 + sizes.root + sizes.metadata, 0,
    127 + sizes.root + sizes.metadata, sizes.tiles, sizes.count, sizes.count, sizes.count];
  numbers.forEach((value, i) => out.writeBigUInt64LE(BigInt(value), 8 + i * 8));
  [1, 1, 1, 1, 0, MAX_ZOOM].forEach((value, i) => out.writeUInt8(value, 96 + i));
  [AREA[0], AREA[1], AREA[2], AREA[3]].forEach((value, i) => out.writeInt32LE(Math.round(value * 1e7), 102 + i * 4));
  out.writeUInt8(14, 118);
  return out;
}

/** The archive as one Buffer. */
function syntheticPmtiles() {
  const all = features();
  const entries = [];
  const blobs = [];
  let offset = 0;
  for (let z = 0; z <= MAX_ZOOM; z++) for (const [, x, y] of tileRange(z)) {
    const data = tile(z, x, y, all);
    entries.push({ id: zxyToTileId(z, x, y), offset, length: data.length, data });
    offset += data.length;
  }
  entries.sort((a, b) => a.id - b.id);
  offset = 0;
  for (const entry of entries) { entry.offset = offset; offset += entry.length; blobs.push(entry.data); }
  const root = directory(entries), metadata = Buffer.from('{"name":"synthetic preview basemap"}');
  const tiles = Buffer.concat(blobs);
  return Buffer.concat([header({ root: root.length, metadata: metadata.length, tiles: tiles.length, count: entries.length }), root, metadata, tiles]);
}

/**
 * An HTTP answer for a Range request on `archive`, as the server gives it (206 or 416); without a
 * Range header the whole file. For Playwright's route.fulfill.
 * @param {Buffer} archive @param {string | undefined} range
 */
function rangeAnswer(archive, range) {
  const headers = { 'content-type': 'application/vnd.pmtiles', 'accept-ranges': 'bytes', etag: '"synthetic-1"', 'cache-control': 'private, max-age=3600' };
  const match = /^bytes=(\d+)-(\d*)$/.exec(range ?? '');
  if (!range) return { status: 200, headers, body: archive };
  const start = match ? Number(match[1]) : archive.length;
  if (!match || start >= archive.length) return { status: 416, headers: { 'content-range': `bytes */${archive.length}` }, body: '' };
  const end = Math.min(archive.length - 1, match[2] ? Number(match[2]) : archive.length - 1);
  return { status: 206, headers: { ...headers, 'content-range': `bytes ${start}-${end}/${archive.length}` }, body: archive.subarray(start, end + 1) };
}

module.exports = { syntheticPmtiles, rangeAnswer, SYNTHETIC_AREA: AREA };
