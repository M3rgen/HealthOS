// The route without a basemap: an SVG line in a flat projection, with start and finish, lap
// boundaries, a scale bar and the shared cursor. Used whenever the map cannot show a basemap, so
// the view is never empty. No request leaves the page.
import { num } from './format.js';
import { text } from './i18n/workout-detail.js';
import { svg } from './workout-charts.js';
import { flatProjection, lapBoundaries, nearestPoint, routeEnds, routeRuns } from './workout-track.js';

const PAD = 22, REACH = 28;

/** A round distance about a quarter of `metres`, for the scale bar. @param {number} metres */
function scaleLength(metres) {
  const target = metres / 4;
  const base = 10 ** Math.floor(Math.log10(Math.max(1, target)));
  return [5, 2, 1].map(step => step * base).find(step => step <= target) ?? base;
}

/** @param {import('./workout-track.js').Track} track @param {number} width @param {number} height */
function layout(track, width, height) {
  const bounds = /** @type {number[]} */ (track.summary.bounds);
  const project = flatProjection(bounds);
  const [east, south] = project(bounds[3], bounds[0]);
  const ratio = Math.min((width - 2 * PAD) / Math.max(east, 1), (height - 2 * PAD) / Math.max(south, 1));
  const left = (width - east * ratio) / 2, top = (height - south * ratio) / 2;
  const { latitude = [], longitude = [] } = track.samples;
  /** @type {([number, number] | null)[]} */
  const points = latitude.map((lat, i) => {
    const lon = longitude[i];
    if (lat == null || lon == null) return null;
    const [x, y] = project(lon, lat);
    return [left + x * ratio, top + y * ratio];
  });
  return { points, ratio };
}

/** @param {SVGSVGElement} root @param {number} ratio @param {number} height */
function scaleBar(root, ratio, height) {
  const metres = scaleLength((root.viewBox.baseVal.width - 2 * PAD) / ratio);
  const length = metres * ratio, y = height - 12;
  svg('path', { d: `M${PAD},${y - 4}V${y}H${PAD + length}V${y - 4}`, class: 'workout-route-scale' }, root);
  const label = svg('text', { x: PAD + length + 6, y: y + 3 }, root);
  label.textContent = metres >= 1000 ? `${num(metres / 1000, 0)} km` : `${num(metres, 0)} m`;
}

/** @param {SVGSVGElement} root @param {import('./workout-track.js').Track} track @param {([number, number] | null)[]} points */
function drawLines(root, track, points) {
  for (const run of routeRuns(track.samples)) {
    const d = run.indexes.map((index, i) => {
      const point = /** @type {[number, number]} */ (points[index]);
      return `${i ? 'L' : 'M'}${point[0].toFixed(1)},${point[1].toFixed(1)}`;
    }).join('');
    svg('path', { d, class: 'workout-route-casing' }, root);
    svg('path', { d, class: 'workout-route-line' }, root);
  }
  for (const index of lapBoundaries(track.laps)) {
    const point = points[index];
    if (point) svg('circle', { cx: point[0], cy: point[1], r: 4, class: 'workout-route-lap' }, root);
  }
  const ends = routeEnds(track.samples);
  if (!ends) return;
  for (const [kind, index] of /** @type {const} */ ([['start', ends.first], ['end', ends.last]])) {
    const point = /** @type {[number, number]} */ (points[index]);
    const mark = svg('circle', { cx: point[0], cy: point[1], r: 6.5, class: `workout-route-${kind}` }, root);
    svg('title', {}, mark).textContent = text[kind];
  }
}

/**
 * Draws the route into `host` and keeps it drawn at the host's size.
 * @param {HTMLElement} host @param {import('./workout-track.js').Track} track @param {(index: number) => void} onPick
 */
export function drawRoute(host, track, onPick) {
  /** @type {([number, number] | null)[]} */
  let points = [];
  let index = -1;
  const cursor = /** @type {SVGCircleElement} */ (svg('circle', { r: 7, class: 'workout-route-cursor', visibility: 'hidden' }));
  function draw() {
    const width = Math.max(160, host.clientWidth), height = Math.max(160, host.clientHeight);
    const root = /** @type {SVGSVGElement} */ (svg('svg', { viewBox: `0 0 ${width} ${height}`, width, height, role: 'img', class: 'workout-route' }));
    svg('title', {}, root).textContent = text.route;
    const drawn = layout(track, width, height);
    points = drawn.points;
    drawLines(root, track, points);
    scaleBar(root, drawn.ratio, height);
    root.append(cursor);
    host.replaceChildren(root);
    place(index);
  }
  /** @param {number} next */
  function place(next) {
    index = next;
    const point = points[next];
    cursor.setAttribute('visibility', point ? 'visible' : 'hidden');
    if (point) { cursor.setAttribute('cx', point[0].toFixed(1)); cursor.setAttribute('cy', point[1].toFixed(1)); }
  }
  /** @param {PointerEvent} event */
  function pick(event) {
    const box = host.getBoundingClientRect();
    const found = nearestPoint(points, event.clientX - box.left, event.clientY - box.top, REACH);
    if (found < 0) return;
    place(found);
    onPick(found);
  }
  host.addEventListener('pointerdown', pick);
  host.addEventListener('pointermove', event => { if (event.pointerType === 'mouse' || event.buttons) pick(event); });
  const observer = new ResizeObserver(() => draw());
  observer.observe(host);
  draw();
  return { setCursor: place, destroy: () => observer.disconnect() };
}
