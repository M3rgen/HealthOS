// A plain dark basemap for the workout map, read from the operator's own Protomaps (v4 schema)
// PMTiles file: land, water, green areas, roads and buildings, with no labels or icons, so the
// style needs no glyph or sprite files (docs/reference/maps.md). The colours sit just above the
// page's own surfaces so the route stays the brightest line.

const COLORS = {
  land: '#1d1d19', water: '#16232a', green: '#1c261b', sand: '#24231d', urban: '#22211d',
  building: '#2b2a24', path: '#3b3933', minor: '#3f3d36', major: '#56524a', highway: '#6b6658',
};
const GREEN = ['park', 'forest', 'wood', 'nature_reserve', 'grass', 'grassland', 'meadow', 'garden', 'golf_course', 'cemetery',
  'playground', 'recreation_ground', 'scrub', 'village_green', 'allotments', 'protected_area', 'national_park'];

/** @param {number} low @param {number} high wide at zoom 18 @returns {import('maplibre-gl').ExpressionSpecification} */
const widths = (low, high) => ['interpolate', ['exponential', 1.6], ['zoom'], 10, low, 18, high];

/**
 * @param {string} id @param {string} kinds the roads' `kind` values @param {string} color @param {[number, number]} width
 * @returns {import('maplibre-gl').LineLayerSpecification}
 */
function roads(id, kinds, color, width) {
  return {
    id, type: 'line', source: 'basemap', 'source-layer': 'roads', minzoom: kinds === 'path' ? 13 : 6,
    filter: ['in', ['get', 'kind'], ['literal', kinds.split(' ')]],
    layout: { 'line-cap': 'round', 'line-join': 'round' },
    paint: { 'line-color': color, 'line-width': widths(...width), ...(kinds === 'path' ? { 'line-dasharray': [2, 1.5] } : {}) },
  };
}

/** @returns {import('maplibre-gl').LayerSpecification[]} */
function baseLayers() {
  const polygons = /** @type {import('maplibre-gl').FilterSpecification} */ (['==', ['geometry-type'], 'Polygon']);
  return [
    { id: 'background', type: 'background', paint: { 'background-color': COLORS.water } },
    { id: 'earth', type: 'fill', source: 'basemap', 'source-layer': 'earth', paint: { 'fill-color': COLORS.land } },
    { id: 'landcover', type: 'fill', source: 'basemap', 'source-layer': 'landcover',
      paint: { 'fill-color': ['match', ['get', 'kind'], ['forest', 'grassland', 'scrub'], COLORS.green, ['barren'], COLORS.sand, COLORS.urban] } },
    { id: 'landuse-green', type: 'fill', source: 'basemap', 'source-layer': 'landuse',
      filter: ['in', ['get', 'kind'], ['literal', GREEN]], paint: { 'fill-color': COLORS.green } },
    { id: 'water', type: 'fill', source: 'basemap', 'source-layer': 'water', filter: polygons, paint: { 'fill-color': COLORS.water } },
    { id: 'waterway', type: 'line', source: 'basemap', 'source-layer': 'water', filter: ['!=', ['geometry-type'], 'Polygon'],
      paint: { 'line-color': COLORS.water, 'line-width': widths(0.5, 6) } },
    { id: 'buildings', type: 'fill', source: 'basemap', 'source-layer': 'buildings', minzoom: 14,
      paint: { 'fill-color': COLORS.building, 'fill-opacity': 0.9 } },
    roads('roads-path', 'path', COLORS.path, [0.4, 2]),
    roads('roads-minor', 'minor_road other', COLORS.minor, [0.4, 9]),
    roads('roads-major', 'major_road', COLORS.major, [0.8, 14]),
    roads('roads-highway', 'highway', COLORS.highway, [1, 18]),
  ];
}

/** @returns {import('maplibre-gl').LayerSpecification[]} */
function routeLayers() {
  return [
    { id: 'route-casing', type: 'line', source: 'route', layout: { 'line-cap': 'round', 'line-join': 'round' },
      paint: { 'line-color': '#141413', 'line-width': 7, 'line-opacity': 0.85 } },
    { id: 'route', type: 'line', source: 'route', layout: { 'line-cap': 'round', 'line-join': 'round' },
      paint: { 'line-color': '#f0c478', 'line-width': 3.5 } },
    { id: 'route-laps', type: 'circle', source: 'marks', filter: ['==', ['get', 'kind'], 'lap'],
      paint: { 'circle-radius': 4, 'circle-color': '#20201c', 'circle-stroke-color': '#f0c478', 'circle-stroke-width': 2 } },
    { id: 'route-ends', type: 'circle', source: 'marks', filter: ['!=', ['get', 'kind'], 'lap'],
      paint: { 'circle-radius': 6.5, 'circle-color': ['match', ['get', 'kind'], 'start', '#48d88c', '#ff7a8a'],
        'circle-stroke-color': '#141413', 'circle-stroke-width': 2 } },
    { id: 'route-cursor', type: 'circle', source: 'cursor',
      paint: { 'circle-radius': 7, 'circle-color': '#f8f8f8', 'circle-stroke-color': '#f0c478', 'circle-stroke-width': 3 } },
  ];
}

/**
 * The whole style: the basemap source from PMTiles and three GeoJSON sources the map fills.
 * @param {string} tiles a pmtiles:// URL; its header gives the zoom range and bounds
 * @returns {import('maplibre-gl').StyleSpecification}
 */
export function mapStyle(tiles) {
  const empty = /** @type {import('geojson').FeatureCollection} */ ({ type: 'FeatureCollection', features: [] });
  return {
    version: 8,
    sources: {
      basemap: { type: 'vector', url: tiles },
      route: { type: 'geojson', data: empty },
      marks: { type: 'geojson', data: empty },
      cursor: { type: 'geojson', data: empty },
    },
    layers: [...baseLayers(), ...routeLayers()],
  };
}
