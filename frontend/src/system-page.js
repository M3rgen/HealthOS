// Sistem page: the panels load when the page opens. A service that does not answer resolves
// every panel to a clear "ulaşılamıyor" text instead of leaving "yükleniyor" on screen.
import { queries } from './breakpoints.js';
import { handleUnauthorized } from './api.js';
import { $, setText } from './dom.js';
import { morphChildren } from './dom-morph.js';
import { createPanelTabs } from './panel-tabs.js';
import { setupCoverage } from './coverage.js';
import { dataTypeLabel } from './data-types.js';
import { clockTime, shortDate, todayKey } from './dates.js';
import { setupDevices } from './devices.js';
import { escape, num } from './format.js';
import { t } from './i18n/index.js';
import { dateFormat, number } from './locale.js';
import { sourceEnabled } from './sources.js';
import { state } from './state.js';
import { setupWebhookStatus } from './webhook-status.js';

const system = t.systemStatus;
// Below this size the four groups no longer fit side by side and become tabs (pages.css).
const SYSTEM_TABS = queries.systemTabs;
const GROUPS = /** @type {const} */ (['status', 'devices', 'sync', 'settings']);

function setupSystemGroups() {
  const host = /** @type {HTMLElement} */ ($('system-panels'));
  createPanelTabs({
    host, prefix: 'system', listLabel: t.systemTabs.label, media: matchMedia(SYSTEM_TABS), initial: 'status',
    items: GROUPS.map(key => {
      const panel = /** @type {HTMLElement} */ (host.querySelector(`[data-group="${key}"]`));
      panel.setAttribute('aria-label', t.systemTabs[key]);
      return { key, label: t.systemTabs[key], panel };
    }),
  });
}
/** @type {Record<string, string>} */
const syncStates = system.syncStates;

/** @typedef {{data_type: string, last_successful_sync?: string | null, status: string, records_downloaded?: number, source?: string}} SyncRow */
/** @typedef {{kinds?: Record<string, {records?: number, last_success_at?: string | null, failing?: boolean}>}} GarminStatus */
/**
 * Garmin's kinds as rows of the same table: the last good pull, whether the newest run failed,
 * and the records archived. Its free-text error is never shown (it can hold file paths).
 * @param {GarminStatus} status
 * @returns {SyncRow[]}
 */
const garminRows = status => Object.entries(status.kinds ?? {}).map(([kind, row]) => ({
  data_type: kind,
  source: 'Garmin',
  last_successful_sync: row.last_success_at ?? null,
  status: row.failing ? 'failed' : row.last_success_at ? 'completed' : 'pending',
  records_downloaded: row.records ?? 0,
}));

/**
 * GitHub-only Apple extension: the Apple Health import panel loads on the first visit to the
 * System page, outside the initial bundle (docs/PERFORMANCE_BUDGET.md).
 * @param {typeof import('./api.js').request} request @param {() => string} getZone
 */
function lazyApplePanel(request, getZone) {
  /** @type {Promise<{refresh: () => Promise<void>}> | null} */
  let panel = null;
  return () => {
    panel ??= import('./apple-import.js').then(module =>
      module.setupAppleImport({ request, getZone, handleUnauthorized, number, dateFormat }),
    );
    panel.then(loaded => loaded.refresh()).catch(() => { panel = null; });
  };
}

/**
 * @param {{request: typeof import('./api.js').request,
 *   notice: (message: string, error?: boolean, source?: string, detail?: string, action?: {label: string, run: () => unknown} | null) => void,
 *   clearNotice: (...sources: string[]) => void, refresh: () => Promise<void>}} parts
 */
export function setupSystemPage({ request, notice, clearNotice, refresh }) {
  setupSystemGroups();
  const getZone = () => state.zone;
  const coverage = setupCoverage({ request, getZone, getToday: () => todayKey(state.zone) });
  const webhook = setupWebhookStatus({ request, getZone });
  const devices = setupDevices({ request, getZone });
  const appleRefresh = lazyApplePanel(request, getZone);

  /** @param {string} text */
  const syncMessage = text => morphChildren($('sync-rows'), `<tr><td colspan="4">${escape(text)}</td></tr>`);
  function paintUnreachable() {
    for (const id of ['coverage-summary', 'webhook-status-summary', 'devices-summary']) setText($(id), system.unreachable);
    setText($('webhook-status-message'), system.unreachable);
    syncMessage(system.unreachable);
  }
  /** @param {SyncRow} row */
  function syncRow(row) {
    const last = row.last_successful_sync
      ? `${escape(shortDate(row.last_successful_sync, state.zone))} ${escape(clockTime(row.last_successful_sync, state.zone))}`
      : '—';
    // Beside Google's jobs a Garmin kind is named with its source: both have "Adım" and "Uyku".
    const label = row.source && sourceEnabled(state.health, 'google') ? `${row.source} · ${dataTypeLabel(row.data_type)}` : dataTypeLabel(row.data_type);
    return `<tr data-key="${escape(`${row.source ?? ''}${row.data_type}`)}"><td>${escape(label)}</td><td>${last}</td><td>${escape(syncStates[row.status] ?? row.status)}</td>` +
      `<td>${num(row.records_downloaded)}</td></tr>`;
  }
  /** @param {SyncRow[]} rows */
  function syncRowsMarkup(rows) {
    return rows.map(syncRow).join('');
  }
  /** The table's rows from every selected source: Google's sync jobs, then Garmin's pulled kinds. */
  async function syncRows() {
    const google = sourceEnabled(state.health, 'google'), garmin = sourceEnabled(state.health, 'garmin');
    const [jobs, pulls] = await Promise.all([
      google ? request('/sync/status') : [],
      garmin ? /** @type {Promise<GarminStatus>} */ (request('/api/garmin/status')).then(garminRows) : [],
    ]);
    return [.../** @type {import('./api-types').SyncStatusRow[]} */ (jobs), ...pulls];
  }
  async function loadSyncRows() {
    const failed = state.health && !sourceEnabled(state.health, 'google') ? system.garmin.kindsFailed : system.syncStatusFailed;
    try {
      const rows = await syncRows();
      if (rows.length) morphChildren($('sync-rows'), syncRowsMarkup(rows));
      else syncMessage(system.noSyncYet);
      clearNotice('sync-status');
    } catch {
      syncMessage(failed);
      notice(failed, true, 'sync-status', '', { label: t.common.retry, run: loadSyncRows });
    }
  }
  async function load() {
    if (!state.health) {
      paintUnreachable();
      return refresh();
    }
    devices.refresh();
    appleRefresh();
    const google = sourceEnabled(state.health, 'google');
    if (google) {
      coverage.refresh();
      webhook.refresh();
    }
    if (google || sourceEnabled(state.health, 'garmin')) await loadSyncRows();
  }
  return { load };
}
