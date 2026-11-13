// Ölçümler page: the list of hand-entered measurements.
import { $ } from './dom.js';
import { replaceWithFocus } from './accessibility.js';
import { fullDateTime } from './dates.js';
import { escape, num } from './format.js';
import { iconMarkup } from './icons.js';
import { t } from './i18n/index.js';
import { METRICS, measurementText } from './measurement-metrics.js';
import { flagCells, isTest } from './measurement-flags.js';
import { setPageStatus } from './page-status.js';
import { state } from './state.js';

export const MEASUREMENT_LIMIT = 100;

/** @typedef {import('./measurement-flags.js').FlaggableRow & {metric: string, value: number, measured_at: string}} MeasurementRow */

/** @param {string} text */
const measurementMessageMarkup = text => `<tr><td colspan="5">${escape(text)}</td></tr>`;
/** @param {MeasurementRow} item */
function row(item) {
  const label = METRICS[item.metric]?.label ?? item.metric ?? '?';
  const when = fullDateTime(item.measured_at, state.zone);
  return `<tr${isTest(item) ? ' class="flagged-row"' : ''}><td>${escape(when)}</td><td><span class="cell-icon">${iconMarkup(item.metric)}</span>${escape(label)}</td>` +
    `<td>${escape(measurementText(item.metric, item.value, 2))}</td>${flagCells(item, label, when)}</tr>`;
}

/** @param {MeasurementRow[]} rows */
function measurementRowsMarkup(rows) {
  let html = rows.length ? rows.map(row).join('') : measurementMessageMarkup(t.measurements.empty);
  // Keep the limit notice inside the table's scroll region, after every measurement.
  if (rows.length >= MEASUREMENT_LIMIT) html += measurementMessageMarkup(t.measurements.limited(num(MEASUREMENT_LIMIT)));
  return html;
}

/** @param {{request: typeof import('./api.js').request, notice: import('./dashboard-data.js').Notify, clearNotice: import('./dashboard-data.js').ClearNotice}} parts */
export function setupMeasurementList({ request, notice, clearNotice }) {
  /** @param {MeasurementRow[] | null} rows */
  function fill(rows) {
    const container = $('measurement-rows');
    replaceWithFocus(container, () => {
      if (rows === null) container.innerHTML = measurementMessageMarkup(t.measurements.loadFailed);
      else container.innerHTML = measurementRowsMarkup(rows);
    }, old => container.querySelector(`[data-flag-id="${CSS.escape(/** @type {string} */ (/** @type {HTMLElement} */ (old).dataset.flagId))}"]`), $('measurements-add'));
  }
  async function load() {
    try {
      /** @type {MeasurementRow[]} */
      const rows = await request('/api/dashboard/measurements');
      fill(rows);
      setPageStatus('measurements', { loadFailed: false, rows: rows.length });
      clearNotice('measurements');
    } catch {
      fill(null);
      setPageStatus('measurements', { loadFailed: true });
      notice(t.measurements.loadFailed, true, 'measurements', '', { label: t.common.retry, run: load });
    }
  }
  return { load };
}
