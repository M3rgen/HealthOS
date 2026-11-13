// "Şimdi eşitle": starts a run, waits for its outcome and reports it in the notice.
import { errorText } from './api.js';
import { $ } from './dom.js';
import { clockTime, dateKey, shortDate } from './dates.js';
import { dataTypeLabel } from './data-types.js';
import { num } from './format.js';
import { t } from './i18n/index.js';
import { state } from './state.js';
import { describeSyncError, rawErrors } from './sync-errors.js';

/** @typedef {typeof import('./api.js').request} Request */
/** @typedef {{sequence: number, inserted: number, updated: number, failed: boolean}} RunResult */
/** @typedef {{data_type: string, last_error?: string | null, last_run?: RunResult | null}} SyncStatusRow */
/** @typedef {{type: string, error?: string | null} & Partial<RunResult>} ObservedRun */
/** @typedef {{type: string, error?: string | null} & RunResult} FinishedRun */

/** @param {number} ms */
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));

/** The newest device sync time as a sentence, or '' when it is not known. @param {Request} request */
async function watchLastSync(request) {
  try {
    /** @type {{devices: {last_sync_time?: string | null}[]}} */
    const { devices } = await request('/google-health/devices');
    const times = devices
      .flatMap(device => device.last_sync_time || [])
      .sort((a, b) => Date.parse(b) - Date.parse(a));
    if (!times[0]) return '';
    const zone = state.zone;
    const day = dateKey(times[0], zone) === dateKey(new Date(), zone) ? '' : shortDate(times[0], zone);
    return ` ${t.sync.watchLast(day, clockTime(times[0], zone))}`;
  } catch {
    return '';
  }
}

/** @param {ObservedRun} run @param {number} baseline @returns {run is FinishedRun} */
const finishedAfter = (run, baseline) => run.sequence !== undefined && run.sequence > baseline;

/**
 * Polls until every type's run has finished; null at the deadline.
 * @param {Request} request @param {{data_types: string[], sequence: number}} started
 * @returns {Promise<FinishedRun[] | null>}
 */
async function waitForRun(request, { data_types: types, sequence }) {
  // A run counts once its in-memory outcome has a sequence above the baseline returned by POST /sync/all.
  for (const deadline = Date.now() + 90000; Date.now() < deadline;) {
    await wait(2000);
    try {
      /** @type {SyncStatusRow[]} */
      const rows = await request('/sync/status');
      /** @type {ObservedRun[]} */
      const runs = types.map(type => {
        const row = rows.find(item => item.data_type === type);
        return { type, error: row?.last_error, ...row?.last_run };
      });
      if (runs.every(run => finishedAfter(run, sequence))) return runs;
    } catch {
      /* Keep polling until the deadline. */
    }
  }
  return null;
}

/** The notice for a finished wait. @param {FinishedRun[] | null} runs @param {Request} request */
async function outcome(runs, request) {
  if (!runs) return { message: t.sync.background, failed: [] };
  const added = runs.reduce((sum, run) => sum + run.inserted, 0);
  const changed = runs.reduce((sum, run) => sum + run.updated, 0);
  const failed = runs.filter(run => run.failed);
  const counts = [added && t.sync.added(num(added)), changed && t.sync.updated(num(changed))].filter(Boolean).join(' · ');
  if (!failed.length) return { message: t.sync.done(counts || `${t.sync.noNewData}${await watchLastSync(request)}`), failed };
  const list = failed.map(run => `${dataTypeLabel(run.type)}: ${describeSyncError(run.error)}`).join(' · ');
  return { message: `${t.sync.partlyFailed(num(failed.length), num(runs.length), list)}${counts ? ` · ${counts}` : ''}`, failed };
}

/**
 * @param {object} parts
 * @param {Request} parts.request
 * @param {import('./dashboard-data.js').Notify} parts.notice
 * @param {import('./dashboard-data.js').ClearNotice} parts.clearNotice
 * @param {() => Promise<unknown>} parts.refresh
 */
export function setupSyncAction({ request, notice, clearNotice, refresh }) {
  const button = $('sync-button');
  let syncing = false;
  /** @param {boolean} on */
  function setBusy(on) {
    syncing = on;
    button.setAttribute('aria-disabled', String(on));
    button.classList.toggle('syncing', on);
    button.setAttribute('aria-busy', String(on));
    /** @type {Element} */ (button.lastElementChild).textContent = on ? t.sync.running : t.sync.now;
  }
  button.addEventListener('click', async () => {
    if (syncing) return;
    setBusy(true);
    clearNotice('sync', 'sync-start', 'sync-result');
    try {
      /** @type {import('./api-types').SyncStarted} */
      const run = await request('/sync/all', { method: 'POST', headers: { 'X-HealthOS-Request': '1' } });
      const { message, failed } = await outcome(await waitForRun(request, run), request);
      await refresh();
      notice(message, failed.length > 0, failed.length ? 'sync' : 'sync-result', rawErrors(failed.map(item => [item.type, item.error])));
    } catch (error) {
      notice(t.sync.startFailed(errorText(error)), true, 'sync-start');
    } finally {
      setBusy(false);
    }
  });
}
