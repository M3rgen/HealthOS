// The workout detail view (Spor › a workout with a track): map, charts, summary and laps in their
// own panel, which takes the timeline's place until "back". Loaded, with its styles and words,
// the first time a track is opened; MapLibre only when a basemap can actually be shown.
import './workout-detail.css';
import { ApiError } from './api.js';
import { setDetailLanguage, text } from './i18n/workout-detail.js';
import { TILES_PATH, hasWebgl2, probeBasemap } from './workout-basemap.js';
import { createCharts, svg } from './workout-charts.js';
import { button, el, header, lapTable, legend, summary, toggles } from './workout-detail-view.js';
import { drawRoute } from './workout-route.js';
import { axes, covers, lapBoundaries, pacedSport, pauses, positions, routeEnds, seriesOf } from './workout-track.js';

// Enough for the map and a screen of charts (docs/reference/garmin-tracks.md recommends 2000).
const MAX_POINTS = 2000;

/**
 * @typedef {{request: typeof import('./api.js').request, row: import('./workouts-markup.js').WorkoutRow,
 *   words: {title: string, when: string}, list: HTMLElement, refresh: () => Promise<void>, gone: (message: string) => void}} DetailOptions
 * @typedef {{setCursor: (index: number) => void, destroy: () => void}} RouteView
 * @typedef {Awaited<ReturnType<typeof import('./workout-map.js').openMap>>} MapView
 */

class WorkoutDetail {
  /** @param {DetailOptions} options */
  constructor(options) {
    setDetailLanguage(document.documentElement.lang);
    this.options = options;
    /** @type {import('./workout-track.js').Track | null} */
    this.track = null;
    /** @type {import('./workout-track.js').Axis} */
    this.axis = 'time';
    this.paced = pacedSport(options.row.exercise_type);
    /** @type {RouteView | null} */
    this.route = null;
    /** @type {MapView | null} */
    this.map = null;
    this.closed = false;
    this.root = el('section', '', 'panel workout-detail');
    this.root.setAttribute('aria-labelledby', 'workout-detail-title');
    this.back = button(text.back, 'workout-detail-back', () => this.close());
    this.status = el('p', '', 'workout-detail-status');
    this.status.setAttribute('role', 'status');
    this.retry = button(text.retry, 'workout-detail-retry', () => this.load());
    this.retry.hidden = true;
    this.body = el('div', '', 'workout-detail-body');
    this.body.hidden = true;
    this.body.dataset.scrollFocus = '';
    const head = el('header', '', 'workout-detail-head');
    head.append(this.back, header(options.row, options.words));
    const state = el('div', '', 'workout-detail-state');
    state.append(this.status, this.retry);
    this.root.append(head, state, this.body);
    this.root.addEventListener('keydown', event => { if (event.key === 'Escape' && !event.defaultPrevented) this.close(); });
  }
  open() {
    this.options.list.hidden = true;
    this.options.list.after(this.root);
    this.back.focus({ preventScroll: true });
    return this.load();
  }
  async load() {
    this.retry.hidden = true;
    this.status.textContent = text.loading;
    this.root.setAttribute('aria-busy', 'true');
    const id = encodeURIComponent(String(this.options.row.id));
    try {
      /** @type {{track: import('./workout-track.js').Track | null}} */
      const answer = await this.options.request(`/api/dashboard/workouts/${id}/track?max_points=${MAX_POINTS}`);
      if (this.closed) return;
      if (answer.track) this.show(answer.track);
      else this.status.textContent = text.noTrack;
    } catch (error) {
      if (!this.closed) await this.failed(error);
    } finally {
      this.root.setAttribute('aria-busy', 'false');
    }
  }
  /** 404: the workout left the list; a timeout or another failure can be tried again. @param {unknown} error */
  async failed(error) {
    if (error instanceof ApiError && error.status === 404) {
      this.close();
      await this.options.refresh();
      this.options.gone(text.gone);
      return;
    }
    const timedOut = error instanceof ApiError && (error.status === 504 || error.kind === 'timeout');
    this.status.textContent = timedOut ? text.timeout : text.failed;
    this.retry.hidden = false;
  }
  /** @param {import('./workout-track.js').Track} track */
  show(track) {
    this.track = track;
    if (!axes(track.samples).includes(this.axis)) this.axis = 'time';
    this.status.textContent = '';
    const side = el('div', '', 'workout-detail-side');
    this.charts = el('div', '', 'workout-charts');
    // A track with times only (no speed, heart rate or elevation) has nothing to chart.
    const charted = seriesOf(track.samples, this.paced).length > 0;
    side.append(summary(track, this.paced), ...(charted ? [this.controls(track), this.charts, legend()] : []));
    if (track.point_count < track.source_point_count)
      side.append(el('p', text.thinned(String(track.point_count), String(track.source_point_count)), 'fine-print'));
    const laps = lapTable(track, this.paced);
    if (laps) side.append(laps);
    this.body.replaceChildren(...this.mapArea(track), side);
    this.body.hidden = false;
    if (!charted) return;
    this.chartView = createCharts(this.charts, index => this.cursor(index, 'chart'));
    this.drawCharts();
  }
  /** @param {import('./workout-track.js').Track} track */
  controls(track) {
    const box = el('div', '', 'workout-detail-controls');
    if (axes(track.samples).length > 1)
      box.append(toggles(text.axis, [['time', text.axisTime], ['distance', text.axisDistance]], this.axis, value => {
        this.axis = value;
        this.drawCharts();
      }));
    if (track.samples.speed && pacedSport(this.options.row.exercise_type))
      box.append(toggles(text.speedShown, [['pace', text.pace], ['speed', text.speed]], this.paced ? 'pace' : 'speed', value => {
        this.paced = value === 'pace';
        this.redrawSpeed();
      }));
    return box;
  }
  drawCharts() {
    const track = /** @type {import('./workout-track.js').Track} */ (this.track);
    const end = track.samples.time[track.samples.time.length - 1] ?? 0;
    this.chartView?.render({
      axis: this.axis, xs: positions(track.samples, this.axis), time: track.samples.time,
      series: seriesOf(track.samples, this.paced), pauses: pauses(track.events, end), boundaries: lapBoundaries(track.laps),
    });
  }
  /** Speed and pace change the summary, the lap table and the first chart together. */
  redrawSpeed() {
    const track = /** @type {import('./workout-track.js').Track} */ (this.track);
    this.body.querySelector('.workout-detail-summary')?.replaceWith(summary(track, this.paced));
    const laps = lapTable(track, this.paced);
    if (laps) this.body.querySelector('.workout-laps')?.replaceWith(laps);
    this.drawCharts();
  }
  /** The route, drawn at once without a basemap; the map replaces it when it can. @param {import('./workout-track.js').Track} track */
  mapArea(track) {
    const area = el('div', '', 'workout-detail-map');
    const note = el('p', '', 'workout-map-note fine-print');
    if (!routeEnds(track.samples) || !track.summary.bounds) {
      note.textContent = text.noPosition;
      area.dataset.map = 'none';
      area.append(note);
      return [area];
    }
    const canvas = el('div', '', 'workout-map-canvas');
    area.dataset.map = 'route';
    area.append(canvas, note);
    this.route = drawRoute(canvas, track, index => this.cursor(index, 'route'));
    this.basemap(area, canvas, note, track);
    return [area];
  }
  /**
   * @param {HTMLElement} area @param {HTMLElement} canvas @param {HTMLElement} note @param {import('./workout-track.js').Track} track
   */
  async basemap(area, canvas, note, track) {
    const reason = await this.basemapReason(track);
    if (this.closed) return;
    if (reason) {
      note.textContent = text[reason];
      area.dataset.map = 'route';
      return;
    }
    note.textContent = text.mapLoading;
    const host = el('div', '', 'workout-map-gl');
    host.setAttribute('aria-label', text.map);
    host.setAttribute('role', 'region');
    area.prepend(host);
    try {
      const { openMap } = await import('./workout-map.js');
      const map = await openMap(host, track, new URL(TILES_PATH, location.href).href, index => this.cursor(index, 'map'), text.map);
      if (this.closed) { map.destroy(); return; }
      this.map = map;
      this.route?.destroy();
      canvas.remove();
      area.append(mapTools(map));
      note.textContent = '';
      area.dataset.map = 'basemap';
      await map.idle();
      area.dataset.mapReady = 'true';
    } catch {
      host.remove();
      note.textContent = text.mapFailed;
    }
  }
  /** Why no basemap is shown, or '' when one can be. @param {import('./workout-track.js').Track} track */
  async basemapReason(track) {
    if (!hasWebgl2()) return 'noWebgl';
    const probe = await probeBasemap(TILES_PATH);
    if (probe.state !== 'ready') return probe.state;
    return covers(probe.bounds, /** @type {number[]} */ (track.summary.bounds)) ? '' : 'outside';
  }
  /** One sample index shown on every chart and on the route. @param {number} index @param {'chart' | 'route' | 'map'} from */
  cursor(index, from) {
    if (from !== 'chart') this.chartView?.setCursor(index);
    if (from !== 'route') this.route?.setCursor(index);
    if (from !== 'map') this.map?.setCursor(index);
  }
  close() {
    if (this.closed) return;
    this.closed = true;
    this.map?.destroy();
    this.route?.destroy();
    this.root.remove();
    this.options.list.hidden = false;
    const id = CSS.escape(String(this.options.row.id));
    const trigger = /** @type {HTMLElement | null} */ (this.options.list.querySelector(`[data-workout-id="${id}"] [data-track-open]`));
    (trigger ?? /** @type {HTMLElement | null} */ (this.options.list.querySelector('input, button')))?.focus({ preventScroll: false });
    if (active === this) active = undefined;
  }
}

/** Zoom and "fit to route", and the basemap's attribution, always visible. @param {MapView} map */
function mapTools(map) {
  const tools = el('div', '', 'workout-map-tools');
  for (const [label, action, mark] of /** @type {const} */ ([[text.zoomIn, map.zoomIn, 'M12 6v12M6 12h12'],
    [text.zoomOut, map.zoomOut, 'M6 12h12'], [text.fit, map.fit, 'M5 9V5h4M15 5h4v4M19 15v4h-4M9 19H5v-4']])) {
    const control = button('', 'workout-map-tool', action);
    control.setAttribute('aria-label', label);
    control.title = label;
    svg('path', { d: mark }, svg('svg', { viewBox: '0 0 24 24', 'aria-hidden': 'true' }, control));
    tools.append(control);
  }
  // The basemap is a Protomaps build of OpenStreetMap (ODbL): both are credited, as Protomaps asks.
  const credit = el('p', '', 'workout-map-credit');
  const links = /** @type {const} */ ([[text.basemap, 'https://protomaps.com'],
    [`© ${text.attribution}`, 'https://www.openstreetmap.org/copyright']]).map(([label, href]) => {
    const link = el('a', label);
    link.href = href;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    return link;
  });
  credit.append(links[0], ' ', links[1]);
  const box = el('div', '', 'workout-map-overlay');
  box.append(tools, credit);
  return box;
}

/** @type {WorkoutDetail | undefined} */
let active;
/** Opens the detail of one listed workout in place of the timeline. @param {DetailOptions} options */
export function openWorkoutDetail(options) {
  active?.close();
  active = new WorkoutDetail(options);
  return active.open();
}
