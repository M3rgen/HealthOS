// Workout detail charts: one SVG per series, drawn at its real pixel size like the metric history
// chart (gradient area under a line, dashed guides with labels). Every chart shares one horizontal
// axis and one cursor, which the arrow keys move when the chart stack has focus.
import { num } from './format.js';
import { text } from './i18n/workout-detail.js';
import { clock, indexAt } from './workout-track.js';

const NS = 'http://www.w3.org/2000/svg';
const LEFT = 46, RIGHT = 10, TOP = 8, BOTTOM = 6;
/** @type {Record<import('./workout-track.js').SeriesKey, string>} */
export const COLORS = {
  speed: 'var(--amber)', pace: 'var(--amber)', heart_rate: 'var(--heart-icon)', altitude: 'var(--step-chart)',
  cadence: 'var(--sleep-icon)', power: 'var(--step-icon)', temperature: 'var(--missing)',
};

/**
 * @typedef {{axis: import('./workout-track.js').Axis, xs: number[], time: number[], series: import('./workout-track.js').Series[],
 *   pauses: [number, number][], boundaries: number[]}} ChartModel
 * @typedef {{key: string, svg: SVGSVGElement, value: HTMLElement, y: (value: number) => number, values: import('./workout-track.js').Column,
 *   cursor: SVGGElement}} DrawnChart
 */

/** An SVG element with its attributes. @param {string} tag @param {Record<string, string | number>} attributes @param {Element} [parent] */
export function svg(tag, attributes, parent) {
  const node = document.createElementNS(NS, tag);
  for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, String(value));
  parent?.append(node);
  return node;
}

/** One value with its unit, as the charts and the cursor read it. @param {string} key @param {number | null | undefined} value */
export function valueText(key, value) {
  if (value == null) return '—';
  const unit = /** @type {Record<string, string>} */ (text.units)[key] ?? '';
  if (key === 'pace') return `${clock(value)} ${unit}`;
  return `${num(value, key === 'speed' || key === 'temperature' ? 1 : 0)} ${unit}`;
}

/** An x position as text: elapsed time or kilometres. @param {import('./workout-track.js').Axis} axis @param {number} x */
export const xText = (axis, x) => (axis === 'time' ? clock(x) : `${num(x / 1000, 2)} ${text.units.km}`);

/** Round tick steps: about one per 90 px. @param {import('./workout-track.js').Axis} axis @param {number} span @param {number} width */
function tickStep(axis, span, width) {
  const steps = axis === 'time' ? [60, 120, 300, 600, 900, 1200, 1800, 3600, 7200, 10800]
    : [100, 200, 500, 1000, 2000, 5000, 10000, 20000, 50000];
  const wanted = span / Math.max(2, Math.floor(width / 90));
  return steps.find(step => step >= wanted) ?? steps[steps.length - 1];
}

/** Value range with a little room, and a y mapping (pace runs downwards: faster is higher). @param {import('./workout-track.js').Series} series @param {number} height */
function scale(series, height) {
  const values = /** @type {number[]} */ (series.values.filter(value => value != null));
  let low = Math.min(...values), high = Math.max(...values);
  const room = (high - low) * 0.08 || Math.max(1, Math.abs(high) * 0.05);
  low -= room; high += room;
  const bottom = height - BOTTOM, span = bottom - TOP;
  const share = (/** @type {number} */ value) => ((value - low) / (high - low)) * span;
  const y = (/** @type {number} */ value) => (series.inverted ? TOP + share(value) : bottom - share(value));
  return { low, high, y };
}

/**
 * The line, cut where a value is missing, and the area under it.
 * @param {number[]} px @param {import('./workout-track.js').Column} values @param {(v: number) => number} y @param {number} bottom
 */
function linePaths(px, values, y, bottom) {
  let line = '', area = '', open = -1;
  const close = (/** @type {number} */ end) => { if (open >= 0) area += `L${px[end].toFixed(1)},${bottom}L${px[open].toFixed(1)},${bottom}Z`; };
  values.forEach((value, i) => {
    if (value == null) { close(i - 1); open = -1; return; }
    const point = `${px[i].toFixed(1)},${y(value).toFixed(1)}`;
    if (open < 0) { open = i; line += `M${point}`; area += `M${point}`; } else { line += `L${point}`; area += `L${point}`; }
  });
  close(values.length - 1);
  return { line, area };
}

/**
 * Pause bands (time axis) or marks (distance axis) and dashed lap boundaries.
 * @param {SVGSVGElement} root @param {ChartModel} model @param {(x: number) => number} at @param {number} height
 */
function marks(root, model, at, height) {
  for (const [start, end] of model.pauses) {
    const from = at(model.axis === 'time' ? start : model.xs[indexAt(model.time, start)]);
    const to = at(model.axis === 'time' ? end : model.xs[indexAt(model.time, end)]);
    svg('rect', { x: from, y: TOP, width: Math.max(2, to - from), height: height - TOP - BOTTOM, class: 'workout-chart-pause' }, root);
  }
  for (const index of model.boundaries) {
    const x = at(model.xs[index]).toFixed(1);
    svg('path', { d: `M${x},${TOP}V${height - BOTTOM}`, class: 'workout-chart-lap' }, root);
  }
}

/** @param {SVGSVGElement} root @param {import('./workout-track.js').Series} series @param {{low: number, high: number, y: (v: number) => number}} range @param {number} width */
function guides(root, series, range, width) {
  [range.low, (range.low + range.high) / 2, range.high].forEach((value, i) => {
    const y = range.y(value).toFixed(1);
    svg('path', { d: `M${LEFT},${y}H${width - RIGHT}`, class: i ? 'workout-chart-guide' : 'workout-chart-base' }, root);
    const label = svg('text', { x: LEFT - 8, y: Number(y) + 4, 'text-anchor': 'end' }, root);
    label.textContent = series.key === 'pace' ? clock(value) : num(value, series.key === 'speed' ? 1 : 0);
  });
}

/**
 * Draws one series into its figure and returns what the cursor needs.
 * @param {HTMLElement} figure @param {import('./workout-track.js').Series} series @param {ChartModel} model @param {(x: number) => number} at
 * @returns {DrawnChart}
 */
function drawSeries(figure, series, model, at) {
  const host = /** @type {HTMLElement} */ (figure.querySelector('.workout-chart-plot'));
  const width = Math.max(200, host.clientWidth), height = Math.max(60, host.clientHeight);
  const root = /** @type {SVGSVGElement} */ (svg('svg', { viewBox: `0 0 ${width} ${height}`, width, height, 'aria-hidden': 'true' }));
  const id = `workout-fill-${series.key}`, color = COLORS[series.key];
  const gradient = svg('linearGradient', { id, x1: 0, y1: 0, x2: 0, y2: 1 }, svg('defs', {}, root));
  svg('stop', { offset: 0, style: `stop-color:${color};stop-opacity:.34` }, gradient);
  svg('stop', { offset: 1, style: `stop-color:${color};stop-opacity:0` }, gradient);
  const range = scale(series, height);
  marks(root, model, at, height);
  guides(root, series, range, width);
  const px = model.xs.map(at), { line, area } = linePaths(px, series.values, range.y, height - BOTTOM);
  svg('path', { d: area, fill: `url(#${id})` }, root);
  svg('path', { d: line, fill: 'none', stroke: color, 'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round' }, root);
  const cursor = /** @type {SVGGElement} */ (svg('g', { class: 'workout-chart-cursor', visibility: 'hidden' }, root));
  svg('path', { d: `M0,${TOP}V${height - BOTTOM}` }, cursor);
  svg('circle', { r: 4.5, cx: 0, cy: 0, fill: color }, cursor);
  host.replaceChildren(root);
  return { key: series.key, svg: root, value: /** @type {HTMLElement} */ (figure.querySelector('.workout-chart-value')), y: range.y, values: series.values, cursor };
}

/** The shared horizontal axis under the charts. @param {HTMLElement} host @param {ChartModel} model @param {(x: number) => number} at */
function drawAxis(host, model, at) {
  const width = Math.max(200, host.clientWidth), first = model.xs[0], last = model.xs[model.xs.length - 1];
  const root = svg('svg', { viewBox: `0 0 ${width} 20`, width, height: 20, 'aria-hidden': 'true' });
  const step = tickStep(model.axis, last - first, width - LEFT - RIGHT);
  for (let tick = Math.ceil(first / step) * step; tick <= last; tick += step) {
    const label = svg('text', { x: at(tick).toFixed(1), y: 14, 'text-anchor': 'middle' }, root);
    label.textContent = model.axis === 'time' ? clock(tick) : `${num(tick / 1000, step < 1000 ? 1 : 0)} ${text.units.km}`;
  }
  host.replaceChildren(root);
}

/** A figure per series (name, value at the cursor, plot). @param {import('./workout-track.js').Series} series */
function figureFor(series) {
  const figure = document.createElement('figure');
  figure.className = 'workout-chart';
  figure.dataset.series = series.key;
  const caption = document.createElement('figcaption'), name = document.createElement('span'), value = document.createElement('span');
  name.textContent = text.series[series.key];
  name.style.setProperty('--series', COLORS[series.key]);
  value.className = 'workout-chart-value';
  caption.append(name, value);
  const plot = document.createElement('div');
  plot.className = 'workout-chart-plot';
  figure.append(caption, plot);
  return figure;
}

/** Moves one chart's cursor line and dot to sample `index` at `x` (null hides it). @param {DrawnChart} chart @param {number} index @param {number | null} x */
function placeOn(chart, index, x) {
  const value = x === null ? null : chart.values[index];
  chart.cursor.setAttribute('visibility', x === null ? 'hidden' : 'visible');
  if (x !== null) chart.cursor.setAttribute('transform', `translate(${x.toFixed(1)},0)`);
  const dot = /** @type {SVGCircleElement} */ (chart.cursor.querySelector('circle'));
  dot.setAttribute('visibility', value == null ? 'hidden' : 'inherit');
  if (value != null) dot.setAttribute('cy', chart.y(value).toFixed(1));
  chart.value.textContent = x === null ? '' : valueText(chart.key, value);
}

/** What the cursor stands on, for the range input's spoken value. @param {ChartModel} model @param {number} index */
const cursorText = (model, index) => [xText(model.axis, model.xs[index]),
  ...model.series.map(series => `${text.series[series.key]} ${valueText(series.key, series.values[index])}`)].join(' · ');

/**
 * The chart stack: draws a model, follows the pointer and the keyboard, and moves the cursor. The
 * stack itself is the slider (role="slider", one stop per sample); its spoken value names the
 * position and every series there, since the charts inside are pictures.
 * @param {HTMLElement} host @param {(index: number) => void} onCursor called for a pick here
 */
export function createCharts(host, onCursor) {
  /** @type {ChartModel | null} */
  let model = null;
  /** @type {DrawnChart[]} */
  let drawn = [];
  let index = -1, width = 0;
  const axisHost = document.createElement('div');
  axisHost.className = 'workout-chart-axis';
  for (const [name, value] of [['role', 'slider'], ['tabindex', '0'], ['aria-label', text.cursor], ['aria-valuemin', '0']])
    host.setAttribute(name, value);
  const at = (/** @type {number} */ x) => {
    const xs = /** @type {ChartModel} */ (model).xs, first = xs[0], span = xs[xs.length - 1] - first || 1;
    return LEFT + ((x - first) / span) * (width - LEFT - RIGHT);
  };
  function draw() {
    if (!model) return;
    const figures = /** @type {HTMLElement[]} */ ([...host.querySelectorAll('.workout-chart')]);
    width = Math.max(200, figures[0]?.querySelector('.workout-chart-plot')?.clientWidth ?? 0);
    drawn = figures.map((figure, i) => drawSeries(figure, /** @type {ChartModel} */ (model).series[i], /** @type {ChartModel} */ (model), at));
    drawAxis(axisHost, model, at);
    place(index);
  }
  /** @param {number} next */
  function place(next) {
    index = model && next >= 0 && next < model.xs.length ? next : -1;
    const x = index < 0 || !model ? null : at(model.xs[index]);
    for (const chart of drawn) placeOn(chart, index, x);
    host.setAttribute('aria-valuenow', String(Math.max(0, index)));
    host.setAttribute('aria-valuetext', model && index >= 0 ? cursorText(model, index) : '');
  }
  /** @param {PointerEvent} event */
  function pick(event) {
    const plot = host.querySelector('.workout-chart-plot');
    if (!model || !plot) return;
    const left = plot.getBoundingClientRect().left, xs = model.xs, first = xs[0], span = xs[xs.length - 1] - first;
    const x = first + ((event.clientX - left - LEFT) / Math.max(1, width - LEFT - RIGHT)) * span;
    place(indexAt(xs, x));
    onCursor(index);
  }
  host.addEventListener('pointerdown', pick);
  host.addEventListener('pointermove', event => { if (event.pointerType === 'mouse' || event.buttons) pick(event); });
  host.addEventListener('keydown', event => {
    const next = stepKeys(event, index, model?.xs.length ?? 0);
    if (next === null) return;
    event.preventDefault();
    place(next);
    onCursor(index);
  });
  new ResizeObserver(() => { if (model && host.clientWidth && Math.abs(host.clientWidth - width) > 1) draw(); }).observe(host);
  return {
    /** @param {ChartModel} next */
    render(next) {
      model = next;
      host.replaceChildren(...next.series.map(figureFor), axisHost);
      host.setAttribute('aria-valuemax', String(Math.max(0, next.xs.length - 1)));
      draw();
    },
    /** @param {number} next */
    setCursor: place,
  };
}

/**
 * The sample a key moves the cursor to, or null for another key. Arrows move a hundredth of the
 * workout (a tenth with Shift or the Page keys), so a few thousand samples stay walkable.
 * @param {KeyboardEvent} event @param {number} index current sample, -1 before the first move @param {number} count
 */
export function stepKeys(event, index, count) {
  if (count < 1) return null;
  if (event.key === 'Home' || event.key === 'End') return event.key === 'Home' ? 0 : count - 1;
  const steps = { ArrowRight: 1, ArrowUp: 1, ArrowLeft: -1, ArrowDown: -1, PageUp: 10, PageDown: -10 };
  const step = /** @type {Record<string, number>} */ (steps)[event.key];
  if (!step) return null;
  const by = step * Math.max(1, Math.round(count / 100)) * (event.shiftKey && Math.abs(step) === 1 ? 10 : 1);
  return Math.min(count - 1, Math.max(0, Math.max(0, index) + by));
}
