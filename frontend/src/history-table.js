// The day table under the overview cards: one row per day and, where another source disagrees,
// a folded sub-row naming it. Precedence only picks which source is shown in the cell; every
// alternative stays visible as text.
import { sourceLabel } from './data-types.js';
import { $, closest, setText } from './dom.js';
import { morphChildren } from './dom-morph.js';
import { civilRange, civilShort, clockTime } from './dates.js';
import { duration, escape, num, percent } from './format.js';
import { t } from './i18n/index.js';

const states = /** @type {Record<string, string>} */ (t.history.states);
const { bpm, ms } = t.common;

/** @type {Record<string, [string, (value: number | null | undefined) => string]>} */
const sourceMetrics = {
  steps: [t.history.metrics.steps, value => num(value)],
  sleep_seconds: [t.history.metrics.sleep, duration],
  resting_bpm: [t.history.metrics.restingHeart, value => bpm(num(value))],
  hrv_ms: [t.history.metrics.hrv, value => ms(num(value, 1))],
  spo2: [t.history.metrics.spo2, value => percent(value)],
};
/** @typedef {import('./state.js').DayRow} DayRow */
/** @param {DayRow} row @param {string} metric @returns {{source: string, label?: string, value: number | null}[]} */
const alternativesOf = (row, metric) => row.sources?.[metric]?.alternatives ?? [];
/** A text badge (never colour alone) naming the shown source, only when another source disagrees. @param {DayRow} row @param {string} metric */
function sourceBadge(row, metric) {
  const shown = row.sources?.[metric];
  if (!shown || !alternativesOf(row, metric).length) return '';
  return ` <span class="source-badge">${escape(sourceLabel(shown.source, shown.label))}</span>`;
}
/** @param {DayRow} row */
function alternativesText(row) {
  return Object.entries(sourceMetrics)
    .flatMap(([metric, [label, format]]) =>
      alternativesOf(row, metric).map(alt => `${label}: ${escape(sourceLabel(alt.source, alt.label))} ${format(alt.value)}`),
    )
    .join(' · ');
}

/** @param {number | null | undefined} value @param {(value: number) => string} format */
const measured = (value, format) => (value == null ? '—' : format(value));
/** @param {DayRow} row */
const statusTone = row => (row.status === 'complete' || row.status === 'partial' ? 'good' : 'warning');

/** @param {DayRow} row */
function dayRow(row) {
  const others = alternativesText(row);
  const subId = `source-alternatives-${row.date}`;
  const toggle = others
    ? `<button type="button" class="source-toggle" aria-expanded="false" aria-controls="${subId}" data-keep="aria-expanded">${escape(t.history.otherSources)}</button>`
    : '';
  const subRow = others
    ? `<tr class="source-alternatives" id="${subId}" data-keep="hidden" hidden><td colspan="7">${others}</td></tr>`
    : '';
  return `<tr class="${row.partial ? 'today-row' : ''}" data-key="${escape(row.date)}">
    <td>${escape(civilShort(row.date))}</td>
    <td>${num(row.steps)}${sourceBadge(row, 'steps')}</td>
    <td>${duration(row.sleep_seconds)}${sourceBadge(row, 'sleep_seconds')}</td>
    <td>${escape(measured(row.resting_bpm, value => bpm(num(value))))}${sourceBadge(row, 'resting_bpm')}</td>
    <td>${escape(measured(row.hrv_ms, value => ms(num(value, 1))))}${sourceBadge(row, 'hrv_ms')}</td>
    <td>${escape(measured(row.spo2, value => percent(value)))}${sourceBadge(row, 'spo2')}</td>
    <td><span class="dot ${escape(statusTone(row))}">${escape(states[row.status ?? ''] ?? t.history.unknownStatus)}</span>${toggle}</td>
  </tr>${subRow}`;
}

/** @param {DayRow[]} days */
function historyRowsMarkup(days) {
  return days.map(dayRow).join('');
}

/**
 * @param {{days: DayRow[], start_date: string, end_date: string, timezone: string}} history
 * @param {{current?: boolean}} [options] Whether this is the seven-day window ending today.
 */
export function paintHistory(history, { current = false } = {}) {
  setText($('history-title'), current ? t.history.last7Days : t.history.selected7Days);
  setText($('range-caption'), civilRange(history.start_date, history.end_date));
  $('export-button').removeAttribute('aria-disabled');
  /** @type {HTMLAnchorElement} */ ($('export-button')).href =
    `/api/dashboard/export.csv?${new URLSearchParams({ days: String(history.days.length), end_date: history.end_date, timezone: history.timezone })}`;
  morphChildren($('history-rows'), historyRowsMarkup(history.days));
}

/** Marks the existing caption without duplicating the suffix on repeated failures. @param {number} receivedAt @param {string} zone */
export function paintHistoryStale(receivedAt, zone) {
  const caption = $('range-caption');
  const range = caption.textContent?.split(' · ')[0] ?? '';
  setText(caption, `${range} · ${t.overview.staleSince(clockTime(receivedAt, zone))}`);
}

/** No response has ever arrived: the caption stops saying "Yükleniyor". */
export function paintHistoryFailed() {
  $('range-caption').textContent = t.overview.cards.notLoaded;
  // Nothing to export: without an href the link is neither focusable nor clickable.
  $('export-button').removeAttribute('href');
  $('export-button').setAttribute('aria-disabled', 'true');
}

export function setupHistoryRows() {
  $('history-rows').addEventListener('click', event => {
    const button = closest(event, '.source-toggle');
    if (!button) return;
    const open = button.getAttribute('aria-expanded') !== 'true';
    button.setAttribute('aria-expanded', String(open));
    const subRow = document.getElementById(button.getAttribute('aria-controls') ?? '');
    if (subRow) subRow.hidden = !open;
  });
}
