// Test-entry flags on manual measurements. A flagged entry stays in the list with its badge and
// is left out of the profile and the 3D body; the server only appends flag events, so marking
// and clearing never change or delete the entry itself.
import { $, closest } from './dom.js';
import { escape } from './format.js';
import { t } from './i18n/index.js';

const flags = t.measurements.flags;

/** @typedef {{id: string | number, flags?: string[]}} FlaggableRow */

/** @param {FlaggableRow} row */
export const isTest = row => (row.flags ?? []).includes('test');

/** The source cell (with the badge) and the action cell of one measurement row. @param {FlaggableRow} row @param {string} label @param {string} when */
export function flagCells(row, label, when) {
  const test = isTest(row);
  const action = test ? flags.unmark : flags.mark;
  // The accessible name starts with the visible text (WCAG 2.5.3, label in name).
  return `<td>${escape(flags.manual)}${test ? ` <span class="dot warning">${escape(flags.testBadge)}</span>` : ''}</td><td>` +
    `<button type="button" class="row-action" data-flag-id="${escape(row.id)}" ` +
      `data-test="${test ? 1 : 0}" aria-label="${escape(flags.actionLabel(action, label, when))}">${escape(action)}</button></td>`;
}

/** @param {{request: typeof import('./api.js').request, notice: import('./dashboard-data.js').Notify, reload: () => Promise<unknown>}} parts */
export function setupMeasurementFlags({ request, notice, reload }) {
  $('measurement-rows').addEventListener('click', async event => {
    const button = closest(event, '[data-flag-id]');
    if (!button || button.getAttribute('aria-disabled') === 'true') return;
    const marking = button.dataset.test !== '1';
    button.setAttribute('aria-disabled', 'true');
    try {
      await request(`/api/dashboard/measurements/${encodeURIComponent(/** @type {string} */ (button.dataset.flagId))}/${marking ? 'flag' : 'unflag'}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-HealthOS-Request': '1' },
        body: JSON.stringify({ flag: 'test' }),
      });
      notice(marking ? flags.marked : flags.unmarked);
      await reload();
    } catch (error) {
      notice(flags.failed(/** @type {Error} */ (error).message), true);
    } finally {
      button.removeAttribute('aria-disabled');
    }
  });
}
