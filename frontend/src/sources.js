import { t } from './i18n/index.js';
import { clockTime, shortDate } from './dates.js';
import { setText } from './dom.js';
import { escape } from './format.js';
import { state } from './state.js';

const system = t.systemStatus;

/** @typedef {{active_sources?: string[], reasons?: string[], configuration?: string, garmin?: {last_success_at?: string | null} | null}} SourceHealth */
/** @param {string} value */
const moment = value => `${shortDate(value, state.zone)} ${clockTime(value, state.zone)}`;

// Older health responses lack source selection; preserve their Google-only UI.
/** @param {SourceHealth | null | undefined} health @param {string} source */
export const sourceEnabled = (health, source) => (health?.active_sources ?? ['google']).includes(source);

// Selected, but this installation has no importer for it: the server names that in `reasons`.
/** @param {SourceHealth} health @param {string} source */
const importerMissing = (health, source) => (health?.reasons ?? []).includes(`${source}_importer_not_installed`);

/** The worse reason wins: a failed pull is named before a late one. */
const GARMIN_REASONS = /** @type {const} */ ([['failed', 'garmin_pull_failed'], ['stale', 'garmin_pull_stale']]);
/** @param {string[]} reasons @param {string | null} last */
function garminKey(reasons, last) {
  const named = GARMIN_REASONS.find(([, reason]) => reasons.includes(reason))?.[0];
  return named ?? (!last && reasons.includes('garmin_pull_uninitialized') ? 'waiting' : 'ok');
}
/**
 * Garmin is pulled on a timer, not on request: /health names a failed, late or not yet run pull in
 * `reasons` and the last good pull in `garmin`. The state word and that moment, both for display.
 * @param {SourceHealth} health
 */
export function garminPull(health) {
  const last = health?.garmin?.last_success_at ?? null;
  const key = garminKey(health?.reasons ?? [], last);
  return { key, problem: key !== 'ok', state: system.garmin.states[key], last: last ? system.garmin.lastPull(moment(last)) : '' };
}

/** @param {SourceHealth} health */
function garminDetail(health) {
  const pull = garminPull(health);
  const detail = pull.last ? `${pull.state} · ${pull.last}` : pull.state;
  return (health.reasons ?? []).includes('garmin_key_file_permissive') ? `${detail} · ${system.garmin.keyPermissive}` : detail;
}

/** @param {SourceHealth} health @returns {[string, string][]} */
export function sourceDetails(health) {
  return [
    [
      system.details.googleConfiguration,
      !sourceEnabled(health, 'google') ? system.notConnected : health.configuration === 'healthy' ? system.ready : system.missing,
    ],
    [system.details.garmin, sourceEnabled(health, 'garmin') ? garminDetail(health) : system.notConnected],
    [system.details.apple, !sourceEnabled(health, 'apple') ? system.notConnected : importerMissing(health, 'apple') ? system.notInstalled : system.selected],
  ];
}

/** @param {SourceHealth} health @param {Document} [root] */
export function paintSources(health, root = document) {
  const google = sourceEnabled(health, 'google');
  /** @template {string} Id @param {Id} id @returns {Id extends keyof import('./dom-ids').ElementsById ? import('./dom-ids').ElementsById[Id] : HTMLElement} */
  const $ = id => /** @type {Id extends keyof import('./dom-ids').ElementsById ? import('./dom-ids').ElementsById[Id] : HTMLElement} */
    (/** @type {unknown} */ (root.getElementById(id)));
  $('sync-button').disabled = !google;
  /** @type {HTMLElement} */ (root.querySelector('[href="/auth/google/start"]')).hidden = !google;
  $('google-empty').hidden = !google || health.configuration === 'healthy';
  for (const id of ['coverage-open', 'webhook-status-open']) /** @type {HTMLButtonElement} */ ($(id)).disabled = !google;
  // Google's own panels mean nothing on an installation that does not use Google.
  for (const selector of ['.coverage-panel', '.webhook-status-panel'])
    /** @type {HTMLElement} */ (root.querySelector(selector)).hidden = !google;
  if (!google) {
    // With Garmin selected its pull state is the panel's note (connection-panel.js), and the
    // Sistem table lists Garmin's kinds (system-page.js).
    const garmin = sourceEnabled(health, 'garmin');
    if (!garmin) setText($('webhook-info'), system.googleOffArchive);
    setText($('coverage-summary'), system.googleOff);
    setText($('coverage-period'), '');
    setText($('webhook-status-summary'), system.googleOff);
    if (!garmin && $('sync-rows').textContent !== system.googleOff) $('sync-rows').innerHTML = `<tr><td colspan="4">${escape(system.googleOff)}</td></tr>`;
  }
}
