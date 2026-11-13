// Empty and error states for Genel bakış and Ölçümler. main.js stays the only place that loads
// data: this module only reads what the data modules published (page-status.js) and turns it into
// a state block with a next step. Loaded separately from index.html.
import { $, setText } from './dom.js';
import { t } from './i18n/index.js';
import { onPageStatus, pageStatus } from './page-status.js';
import { paintSample } from './sample-data.js';
import { sourceEnabled } from './sources.js';
import { state } from './state.js';

const text = t.pageStates;

/** @typedef {{kind: string, title: string, detail: string, action?: string, onAction?: () => void}} PageState */

// The texts sit in role="status" regions: a repaint with the same state must not touch them.
/** @param {string} prefix @param {PageState | null} state */
function showState(prefix, state) {
  const box = $(`${prefix}-state`);
  const action = $(`${prefix}-state-action`);
  box.hidden = !state;
  box.dataset.state = state?.kind ?? '';
  if (!state) return;
  setText($(`${prefix}-state-title`), state.title);
  setText($(`${prefix}-state-detail`), state.detail);
  action.hidden = !state.action;
  setText(action, state.action ?? '');
  action.onclick = state.onAction ?? null;
}

// Asks main.js to fetch again, like the notice's retry; a reload would lose the selected day,
// the open sheet and the page's place.
const retry = () => document.dispatchEvent(new Event('healthos:retry'));
const addMeasurement = () => $('measurements-add').click();

/** What an empty archive can actually do next, for this installation's source selection. @returns {{detail: string, action: string, onAction: () => void}} */
function nextStep() {
  const { health } = state;
  if (!health) return { detail: text.overviewEmptyDetail, action: text.addMeasurement, onAction: addMeasurement };
  if (sourceEnabled(health, 'google'))
    return health.configuration === 'healthy'
      ? { detail: text.overviewEmptyDetail, action: text.syncNow, onAction: () => $('sync-button').click() }
      : { detail: text.overviewEmptyDetail, action: text.connectGoogle, onAction: () => location.assign('/auth/google/start') };
  const garmin = sourceEnabled(health, 'garmin');
  return { detail: garmin ? text.overviewEmptyGarmin : text.overviewEmptyNoSource, action: text.addMeasurement, onAction: addMeasurement };
}

/** @returns {PageState | null} */
function overviewState() {
  const { loadFailed, rows, hasValues } = pageStatus('overview');
  if (loadFailed && !rows)
    return { kind: 'error', title: text.overviewError, detail: text.overviewErrorDetail, action: text.retry, onAction: retry };
  if (rows && !hasValues) return { kind: 'empty', title: text.overviewEmpty, ...nextStep() };
  return null;
}

/** @returns {PageState | null} */
function measurementsState() {
  const { loadFailed, rows } = pageStatus('measurements');
  if (loadFailed)
    return { kind: 'error', title: text.measurementsError, detail: text.overviewErrorDetail, action: text.retry, onAction: retry };
  if (rows === 0)
    return { kind: 'empty', title: text.measurementsEmpty, detail: t.measurements.empty, action: text.addMeasurement, onAction: addMeasurement };
  return null;
}

function update() {
  showState('overview', overviewState());
  showState('measurements', measurementsState());
  // CSS hides the bare table when the state block replaces it.
  $('measurements').toggleAttribute('data-state-open', !$('measurements-state').hidden);
  paintSample($('overview-state').dataset.state === 'empty');
}

onPageStatus(update);
update();
