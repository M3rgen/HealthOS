import { ApiError, request } from './api.js';
import { focusVisible } from './accessibility.js';
import { civilFull, civilRange, civilShort } from './dates.js';
import { $ } from './dom.js';
import { duration, escape, num } from './format.js';
import { t } from './i18n/index.js';
import { smooth } from './sparklines.js';
import { metrics, showLoading } from './metric-history.js';

/** @typedef {'steps' | 'sleep' | 'heart'} Metric */
/** @typedef {{date: string, value: number | null, partial?: boolean}} HistoryDay */
/** @typedef {HistoryDay & {value: number}} PresentDay */
/** @typedef {{days: HistoryDay[], start_date: string, end_date: string, earliest_date?: string | null, total_pages: number, page: number}} HistoryPage */

const text = t.metricHistory;
/** @param {HistoryDay} day @returns {day is PresentDay} */
const isPresent = day => day.value != null;

/** @param {Metric} metric @param {number | null} value */
function valueLabel(metric, value) {
  if (value == null) return '—';
  if (metric === 'sleep') return duration(value);
  return metric === 'heart' ? text.bpm(num(value, 1)) : text.steps(num(value));
}

// Drawn at the plot's real pixel size so text and strokes are never stretched.
/** @param {Metric} metric @param {HistoryDay[]} days @param {number} [width] @param {number} [height] */
function metricHistoryChartMarkup(metric, days, width = 1020, height = 236) {
  const values = days.filter(isPresent).map(day => day.value);
  if (!values.length) return `<p class="history-empty">${escape(text.empty)}</p>`;
  const W = Math.max(280, Math.round(width)), H = Math.max(140, Math.round(height));
  const compact = W < 560;
  const high = Math.max(...values, 1) * 1.12;
  const low = metric === 'heart' ? Math.max(0, Math.min(...values) - 8) : 0;
  const max = metric === 'heart' ? Math.max(...values) + 8 : high;
  const tick = (/** @type {number} */ v) => metric === 'sleep' ? text.hoursAxis(num(v / 3600, 1)) : num(v);
  const left = Math.max(36, Math.max(...[low, max].map(v => tick(v).length)) * 6.6 + 16), right = W - 8, top = 12, bottom = H - 28;
  const step = (right - left - 16) / (days.length - 1 || 1);
  const x = (/** @type {number} */ i) => left + 8 + i * step;
  const y = (/** @type {number} */ v) => bottom - (v - low) / (max - low) * (bottom - top);
  const color = metrics[metric].color;
  const title = (/** @type {HistoryDay} */ day) => `${civilFull(day.date)}: ${valueLabel(metric, day.value)}${day.partial ? text.partial : ''}`;
  const defs = `<defs><linearGradient id="history-fill" gradientUnits="userSpaceOnUse" x1="0" y1="${top}" x2="0" y2="${bottom}">` +
    `<stop offset="0" style="stop-color:${color}"/><stop offset="1" style="stop-color:${color};stop-opacity:${metric === 'heart' ? 0 : .42}"/></linearGradient></defs>`;
  let marks = '';
  if (metric === 'heart') {
    /** @type {[number, number][]} */
    let points = [];
    const flush = () => {
      if (points.length > 1) marks = `<path d="${smooth(points)}V${bottom}H${points[0][0]}Z" ` +
        `fill="url(#history-fill)" opacity=".5"/>` +
        `<path d="${smooth(points)}" fill="none" stroke="${color}" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>` + marks;
      points = [];
    };
    days.forEach((day, i) => {
      if (day.value == null) { flush(); return; }
      points.push([x(i), y(day.value)]);
      marks += `<circle cx="${x(i)}" cy="${y(day.value)}" r="${compact ? 3 : 3.5}" ` +
        `fill="var(--surface-2)" stroke="${color}" stroke-width="2"${day.partial ? ' stroke-dasharray="2 2"' : ''}>` +
        `<title>${escape(title(day))}</title></circle>`;
    });
    flush();
  } else {
    const w = Math.max(3, Math.min(22, step * .62)), r = Math.min(4, w / 2);
    days.forEach((day, i) => {
      if (day.value == null) return;
      const top = y(day.value), h = Math.max(1, bottom - top), bx = x(i) - w / 2, rr = Math.min(r, h);
      marks += `<path d="M${bx},${bottom}V${top + rr}Q${bx},${top} ${bx + rr},${top}` +
        `H${bx + w - rr}Q${bx + w},${top} ${bx + w},${top + rr}V${bottom}Z" fill="url(#history-fill)" opacity="${day.partial ? '.5' : '1'}">` +
        `<title>${escape(title(day))}</title></path>`;
    });
  }
  const axis = [low, (max + low) / 2, max].map((v, i) => `<path d="M${left} ${y(v)}H${right}" stroke="var(--line)" ${i ? 'stroke-dasharray="2 4" opacity=".8"' : ''}/>` +
    `<text x="${left - 10}" y="${y(v) + 4}" text-anchor="end">${escape(tick(v))}</text>`).join('');
  const ticks = compact ? [0, 10, 20, days.length - 1] : [0, 6, 12, 18, 24, days.length - 1];
  const shown = ticks.filter((i, at, all) => i < days.length && all.indexOf(i) === at);
  const labels = shown.map(i => `<text x="${x(i)}" y="${H - 6}" text-anchor="middle">${escape(civilShort(days[i].date))}</text>`).join('');
  return `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" aria-hidden="true">${defs}${axis}${marks}${labels}</svg>`;
}

/** @param {Metric} metric @param {HistoryDay} day */
function dayItem(metric, day) {
  const item = document.createElement('li');
  if (day.value == null) item.className = 'missing-day';
  if (day.partial) item.classList.add('partial-day');
  const label = document.createElement('span'); label.textContent = civilShort(day.date);
  const value = document.createElement('strong'); value.textContent = valueLabel(metric, day.value);
  item.title = `${civilFull(day.date)}${day.partial ? text.partial : ''}${day.value == null ? text.missing : ''}`;
  item.append(label, value);
  return item;
}

/** @param {unknown} error @param {() => void} retry */
function showLoadFailure(error, retry) {
  $('metric-history-status').textContent = error instanceof ApiError && ['server', 'rejected'].includes(error.kind) ? text.fetchFailed : text.loadFailed;
  const button = document.createElement('button');
  button.type = 'button'; button.className = 'button outline'; button.textContent = text.retry;
  button.addEventListener('click', retry);
  $('metric-history-status').append(' ', button);
}

/** @param {Metric} metric @param {HistoryPage} result @param {HistoryDay[]} days @param {PresentDay[]} present */
function paintSummary(metric, result, days, present) {
  $('metric-history-range').textContent = civilRange(result.start_date, result.end_date);
  $('metric-history-average-label').textContent = metrics[metric].average;
  $('metric-history-average').textContent = valueLabel(metric, present.length ? present.reduce((sum, day) => sum + day.value, 0) / present.length : null);
  $('metric-history-coverage').textContent = text.daysLogged(present.length, days.length);
  $('metric-history-archive').textContent = result.earliest_date ? text.archiveStart(civilFull(result.earliest_date)) : text.archiveEmpty;
}

/** @param {Metric} metric @param {HistoryDay[]} days */
function paintResult(metric, days) {
  const plot = $('metric-history-chart');
  $('metric-history-content').hidden = false;
  plot.innerHTML = metricHistoryChartMarkup(metric, days, plot.clientWidth, plot.clientHeight);
  plot.setAttribute('aria-label', `${metrics[metric].title}, ${$('metric-history-range').textContent}`);
  $('metric-history-days').replaceChildren(...days.map(day => dayItem(metric, day)));
  $('metric-history-content').hidden = false;
  $('metric-history-status').hidden = true;
}

/**
 * The numbered page buttons with their previous/next and the gaps between far pages.
 * @param {number} current @param {number} total @param {(page: number) => void} onPage
 */
function paintPagination(current, total, onPage) {
  const nav = $('metric-history-pages');
  nav.replaceChildren();
  const pages = [...new Set([1, total, current - 2, current - 1, current, current + 1, current + 2])]
    .filter(page => page >= 1 && page <= total).sort((a, b) => a - b);
  /** @param {string} label @param {number} page @param {boolean} [disabled] */
  const button = (label, page, disabled = false) => {
    const el = document.createElement('button');
    el.type = 'button'; el.textContent = label; el.disabled = disabled;
    // Which control this is, so a page turned from the keyboard puts focus back on it.
    el.dataset.pager = label === text.previous ? 'previous' : label === text.next ? 'next' : String(page);
    el.setAttribute('aria-label', label === String(page) ? text.page(page) : label);
    if (page === current && label === String(page)) el.setAttribute('aria-current', 'page');
    el.addEventListener('click', () => onPage(page));
    nav.append(el);
  };
  button(text.previous, current - 1, current === 1);
  pages.forEach((page, i) => {
    if (i && page - pages[i - 1] > 1) { const gap = document.createElement('span'); gap.textContent = '…'; nav.append(gap); }
    button(String(page), page);
  });
  button(text.next, current + 1, current === total);
  $('metric-history-page-note').textContent = text.pageNote(current, total);
}

/** The pager control holding focus ('previous', 'next' or a page number), or null. */
function pagerFocus() {
  const focused = document.activeElement;
  return focused instanceof HTMLElement && $('metric-history-pages').contains(focused) ? focused.dataset.pager ?? null : null;
}

/**
 * The emptied pager sent focus to the metric's tab; the new one takes it back on the same control,
 * or on the current page when that control is now off (the last page's "Sonraki").
 * @param {string | null} pager @param {number} page
 */
function restorePagerFocus(pager, page) {
  if (!pager) return;
  const nav = $('metric-history-pages');
  /** @type {(key: string) => HTMLButtonElement | null} */
  const find = key => nav.querySelector(`button[data-pager="${key}"]:not(:disabled)`);
  focusVisible(find(pager) ?? find(String(page)));
}

/** @param {unknown} error */
const isUnauthorized = error => error instanceof ApiError && error.kind === 'unauthorized';

/** @param {() => string} getTimezone */
export function setupMetricHistoryContent(getTimezone) {
  const dialog = $('metric-history-dialog');
  /** @type {Metric} */
  let currentMetric = 'steps';
  let currentPage = 1, totalPages = 1;
  /** @type {AbortController | null} */
  let controller = null;
  /** @type {{metric: Metric, days: HistoryDay[]} | null} */
  let plotted = null;
  // Redraw at the new pixel size when the dialog or viewport resizes.
  new ResizeObserver(() => {
    const plot = $('metric-history-chart');
    if (plotted && dialog.open && plot.clientWidth) plot.innerHTML = metricHistoryChartMarkup(plotted.metric, plotted.days, plot.clientWidth, plot.clientHeight);
  }).observe($('metric-history-chart'));

  /** @param {Metric} metric @param {number} [page] */
  async function load(metric, page = 1) {
    controller?.abort();
    const pending = new AbortController(); controller = pending;
    currentMetric = metric; currentPage = page;
    const pager = pagerFocus();
    showLoading(metric, dialog);
    try {
      const query = new URLSearchParams({metric, page: String(page), timezone: getTimezone()});
      /** @type {HistoryPage} */
      const result = await request(`/api/dashboard/history?${query}`, {signal: pending.signal});
      if (controller !== pending || !dialog.open) return;
      const days = [...result.days].sort((a, b) => a.date.localeCompare(b.date));
      const present = days.filter(isPresent);
      totalPages = result.total_pages; currentPage = result.page;
      paintSummary(metric, result, days, present);
      plotted = {metric, days};
      paintResult(metric, days);
      paintPagination(currentPage, totalPages, target => load(currentMetric, target));
      restorePagerFocus(pager, currentPage);
    } catch (error) {
      if (controller !== pending || !dialog.open || pending.signal.aborted || isUnauthorized(error)) return;
      showLoadFailure(error, () => load(metric, page));
    }
  }

  dialog.addEventListener('close', () => {
    controller?.abort(); controller = null;
  });
  return { load };
}
