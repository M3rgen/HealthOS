import { setupDialog } from './accessibility.js';
import { errorText } from './api.js';
import { zonedInputValue, zonedInstant } from './dates.js';
import { $, closest } from './dom.js';
import { t } from './i18n/index.js';
import { state } from './state.js';

/** @type {Record<string, [number, number]>} */
const BOUNDS = { height: [30, 260], weight: [1, 500], body_fat: [0, 100], muscle: [0, 100] };

/** @typedef {Record<string, import('./state.js').Measurement | undefined>} Profile */

function setBounds() {
  const [min, max] = BOUNDS[$('measurement-metric').value];
  $('measurement-value').min = String(min);
  $('measurement-value').max = String(max);
}

/** Empties the form for `metric`, dated now in the selected zone, with its latest value filled in. @param {string} metric @param {Profile | undefined} profile */
function prepareForm(metric, profile) {
  $('measurement-form').reset();
  $('form-error').textContent = '';
  $('measurement-metric').value = metric;
  const local = zonedInputValue(new Date(), state.zone);
  $('measurement-date').value = local;
  $('measurement-date').max = local;
  $('input-timezone').textContent = t.measurements.timezone(state.zone);
  const latest = profile?.[metric];
  if (latest) $('measurement-value').value = String(latest.value);
  $('measurement-form').querySelectorAll('[aria-invalid]').forEach(field => field.removeAttribute('aria-invalid'));
  setBounds();
}

/** @param {typeof import('./api.js').request} request */
function postMeasurement(request) {
  return request('/api/dashboard/measurements', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-HealthOS-Request': '1' },
    body: JSON.stringify({
      metric: $('measurement-metric').value,
      value: $('measurement-value').value,
      measured_at: zonedInstant($('measurement-date').value, state.zone).toISOString(),
    }),
  });
}

/**
 * @param {object} parts
 * @param {typeof import('./api.js').request} parts.request
 * @param {() => Profile | undefined} parts.getProfile
 * @param {import('./dashboard-data.js').Notify} parts.notice
 * @param {() => Promise<unknown>} parts.onSaved
 */
export function setupMeasurements({ request, getProfile, notice, onSaved }) {
  let saving = false;
  const modal = setupDialog($('measurement-dialog'));
  /** @param {string} [metric] */
  function openMeasurement(metric = 'weight') {
    prepareForm(metric, getProfile());
    modal.open({ initial: $('measurement-value') });
  }
  $('add-measurement').addEventListener('click', () => openMeasurement());
  $('measurements-add').addEventListener('click', () => openMeasurement());
  $('profile-values').addEventListener('click', event => {
    const el = closest(event, '[data-metric]');
    if (el) openMeasurement(el.dataset.metric);
  });
  ['measurement-close', 'measurement-cancel'].forEach(id =>
    $(id).addEventListener('click', () => $('measurement-dialog').close()),
  );
  $('measurement-metric').addEventListener('change', () => {
    setBounds();
    $('measurement-value').value = String(getProfile()?.[$('measurement-metric').value]?.value ?? '');
  });
  $('measurement-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (saving) return;
    saving = true;
    $('measurement-save').setAttribute('aria-disabled', 'true');
    $('form-error').textContent = '';
    try {
      await postMeasurement(request);
      $('measurement-dialog').close();
      notice(t.measurements.saved);
      await onSaved();
    } catch (error) {
      $('form-error').textContent = errorText(error);
    } finally {
      saving = false;
      $('measurement-save').removeAttribute('aria-disabled');
    }
  });
  $('measurement-form').addEventListener('invalid', event => /** @type {Element} */ (event.target).setAttribute('aria-invalid', 'true'), true);
  $('measurement-form').addEventListener('input', event => /** @type {Element} */ (event.target).removeAttribute('aria-invalid'));
}
