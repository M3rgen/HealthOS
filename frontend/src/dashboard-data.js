// One refresh of the dashboard: /api/dashboard and /health in parallel, each painted on its own
// and each failure reported on its own. refresh() never rejects and always releases its busy
// flag, so a malformed response or a paint error cannot leave the page stuck or stop the polling.
import { t } from './i18n/index.js';
import { setPageStatus } from './page-status.js';
import { state } from './state.js';
import { describeSyncError, rawErrors } from './sync-errors.js';
import { dataTypeLabel } from './data-types.js';
import { paintHistoryStale } from './history-table.js';

/** @typedef {import('./state.js').DashboardData} DashboardData */
/** @typedef {import('./state.js').Health} Health */

/** The fields paint code relies on. A response without them counts as a failed load. */
/** @param {unknown} value @returns {value is DashboardData} */
export const isDashboardPayload = value =>
  Boolean(value &&
    Array.isArray((/** @type {Partial<DashboardData>} */ (value)).days) &&
    (/** @type {DashboardData} */ (value)).days.length &&
    (/** @type {Partial<DashboardData>} */ (value)).charts &&
    (/** @type {Partial<DashboardData>} */ (value)).profile);

/** @typedef {(message: string, error?: boolean, source?: string, detail?: string, action?: {label: string, run: () => unknown} | null) => void} Notify */
/** @typedef {(...sources: string[]) => void} ClearNotice */
/** @typedef {{data: (value: DashboardData) => void, dataFailed: () => void, health: (value: Health) => void, healthFailed: () => void}} Painters */

/**
 * @param {PromiseSettledResult<unknown>} result
 * @param {{paint: Painters, notice: Notify, clearNotice: ClearNotice, retry: () => unknown, report: (error: unknown) => void}} parts
 */
function paintData(result, { paint, notice, clearNotice, retry, report }) {
  if (result.status === 'fulfilled' &&
    isDashboardPayload(result.value)) {
    try {
      paint.data(result.value);
      state.data = result.value;
      state.dataReceivedAt = Date.now();
      document.querySelector('.metric-cards')?.toggleAttribute('data-stale', false);
      setPageStatus('overview', { loadFailed: false });
      clearNotice('dashboard');
      return false;
    } catch (error) {
      report(error);
    }
  }
  setPageStatus('overview', { loadFailed: true });
  if (!state.data) paint.dataFailed();
  else {
    document.querySelector('.metric-cards')?.toggleAttribute('data-stale', true);
    paintHistoryStale(state.dataReceivedAt, state.zone);
  }
  notice(t.overview.loadFailed, true, 'dashboard', '', { label: t.common.retry, run: retry });
  return true;
}

/**
 * @param {PromiseSettledResult<Health>} result
 * @param {{paint: Painters, onHealthFirstKnown: () => void, report: (error: unknown) => void}} parts
 */
function paintHealth(result, { paint, onHealthFirstKnown, report }) {
  if (result.status === 'fulfilled' &&
    result.value &&
    typeof result.value === 'object') {
    const sourceUnknown = !state.health;
    try {
      paint.health(result.value);
      state.health = result.value;
      // The empty page's next step depends on the source selection, which is known only now.
      setPageStatus('overview', {});
      if (sourceUnknown) onHealthFirstKnown();
      return;
    } catch (error) {
      report(error);
    }
  }
  state.health = null;
  paint.healthFailed();
}

/** @param {{request: typeof import('./api.js').request, notice: Notify, clearNotice: ClearNotice}} parts @param {boolean} dashboardFailed */
async function reportSyncFailures({ request, notice, clearNotice }, dashboardFailed) {
  if (dashboardFailed || !state.health) return;
  if (!((state.health.failed_syncs ?? 0) > 0)) {
    clearNotice('sync');
    return;
  }
  try {
    /** @type {import('./api-types').SyncStatusRow[]} */
    const rows = await request('/sync/status');
    const failed = rows.filter(row => row.status === 'failed' || row.last_error);
    if (failed.length)
      notice(
        t.sync.errors(failed.map(row => `${dataTypeLabel(row.data_type)}: ${describeSyncError(row.last_error)}`).join(' · ')),
        true,
        'sync',
        rawErrors(failed.map(row => [row.data_type, row.last_error])),
      );
  } catch {
    notice(t.sync.detailsUnavailable, true, 'sync');
  }
}

/**
 * @param {object} parts
 * @param {typeof import('./api.js').request} parts.request
 * @param {() => Promise<import('./state.js').Health>} parts.fetchHealth
 * @param {() => URLSearchParams} parts.query
 * @param {Painters} parts.paint
 * @param {Notify} parts.notice
 * @param {ClearNotice} parts.clearNotice
 * @param {() => void} parts.onHealthFirstKnown
 * @param {() => void} parts.afterRefresh
 * @param {() => unknown} parts.retry
 * @param {(error: unknown) => void} [parts.report]
 */
export function createDashboardData({
  request, fetchHealth, query, paint, notice, clearNotice, onHealthFirstKnown, afterRefresh, retry,
  report = console.error,
}) {
  async function refresh() {
    if (state.busy) {
      state.pendingRefresh = true;
      return;
    }
    state.busy = true;
    try {
      const [dashboard, health] = await Promise.allSettled([request(`/api/dashboard?${query()}`), fetchHealth()]);
      const dashboardFailed = paintData(dashboard, { paint, notice, clearNotice, retry, report });
      paintHealth(health, { paint, onHealthFirstKnown, report });
      await reportSyncFailures({ request, notice, clearNotice }, dashboardFailed);
      afterRefresh();
    } catch (error) {
      report(error);
    } finally {
      state.busy = false;
      if (state.pendingRefresh) {
        state.pendingRefresh = false;
        refresh();
      }
    }
  }
  return { refresh };
}
