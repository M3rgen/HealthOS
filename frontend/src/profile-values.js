import { shortDate } from './dates.js';
import { escape } from './format.js';
import { setAttr, setText } from './dom.js';
import { t } from './i18n/index.js';
import { METRICS, measurementText } from './measurement-metrics.js';

/** @param {{iconMarkup: (name: string) => string, getZone: () => string}} parts */
export function setupProfileValues({ iconMarkup, getZone }) {
  const container = /** @type {HTMLElement} */ (document.getElementById('profile-values'));
  const rows = Object.entries(METRICS).map(([key, { label }]) => {
    const row = document.createElement('div');
    row.className = 'measurement';
    // These controls retain their identity across polling and modal saves, so
    // native dialog return focus still has its original, connected opener.
    row.innerHTML = `<label for="profile-${escape(key)}"><span class="measure-icon">${iconMarkup(key)}</span>${escape(label)}</label>` +
      `<button type="button" id="profile-${escape(key)}" data-metric="${escape(key)}" aria-haspopup="dialog"></button><small></small>`;
    container.append(row);
    // A value that has not arrived (or never will, with the service down) reads "—", not blank.
    const button = /** @type {HTMLElement} */ (row.querySelector('button'));
    button.textContent = '—';
    button.setAttribute('aria-label', t.overview.profile.addMeasurement(label, '—'));
    return { key, label, button, note: /** @type {HTMLElement} */ (row.querySelector('small')) };
  });
  return {
    /** @param {Record<string, import('./state.js').Measurement | undefined>} profile */
    update(profile) {
      for (const { key, label, button, note } of rows) {
        const item = profile[key];
        const value = item ? measurementText(key, item.value) : '—';
        setText(button, value);
        setAttr(button, 'aria-label', t.overview.profile.addMeasurement(label, value));
        setText(note, item
          ? shortDate(item.measured_at, getZone())
          : key === 'muscle'
            ? t.overview.profile.manualOnly
            : t.overview.profile.noMeasurement);
      }
    },
  };
}
