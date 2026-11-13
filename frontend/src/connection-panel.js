// The overview's "Veri bağlantısı" panel and the System page's connection list, from /health.
import { $ } from './dom.js';
import { clockTime, fullDate, shortDate } from './dates.js';
import { escape, num } from './format.js';
import { t } from './i18n/index.js';
import { paintLoginWarning } from './login-warning.js';
import { garminPull, paintSources, sourceDetails, sourceEnabled } from './sources.js';
import { state } from './state.js';

const { connection } = t.overview;
const system = t.systemStatus;

/** @typedef {import('./state.js').Health} Health */
/** @param {string | null | undefined} value */
const shortMoment = value => (value ? `${shortDate(value, state.zone)} ${clockTime(value, state.zone)}` : '—');

/** @param {string | null | undefined} value */
function timeHtml(value) {
  return value ? `${escape(clockTime(value, state.zone))}<small>${escape(fullDate(value, state.zone))}</small>` : '—';
}
/** @param {string} id @param {string} text @param {boolean} good */
function setState(id, text, good) {
  const element = /** @type {HTMLElement} */ (document.getElementById(id));
  element.textContent = text;
  element.className = good ? 'good' : 'warning';
}

/**
 * The report's "last write" line. A report without the field (the 503 sent while the database is
 * down) says nothing about the archive; only an explicit null means nothing was ever saved.
 * @param {string | null | undefined} moment
 */
function writeHtml(moment) {
  if (moment === undefined) return escape(connection.statusUnknown);
  if (moment === null) return `${escape(connection.noWrite)}<small>${escape(connection.noWriteNote)}</small>`;
  return `${escape(connection.lastWrite(fullDate(moment, state.zone), clockTime(moment, state.zone)))}<small>${escape(connection.lastWriteNote)}</small>`;
}

/** Service wording and colour for a health report's overall status. @param {string} status */
function serviceView(status) {
  if (status === 'healthy') return { text: connection.serviceHealthy, tone: 'good', detail: system.running };
  if (status === 'failed') return { text: connection.serviceFailed, tone: 'bad', detail: system.error };
  return { text: connection.serviceCheck, tone: 'warning', detail: system.check };
}

/** @param {string | undefined} scheduler */
function schedulerText(scheduler) {
  if (scheduler === 'running') return connection.running;
  return scheduler === 'disabled' ? connection.disabled : connection.stopped;
}

/** @param {Health} health @param {ReturnType<typeof serviceView>} service */
function paintConnectionPanel(health, service) {
  $('service-status').textContent = service.text;
  $('service-status').className = `status-text ${service.tone}`;
  $('last-sync').innerHTML = timeHtml(health.last_sync_success);
  $('last-heart').innerHTML = timeHtml(health.data_freshness?.heart_rate);
  $('last-steps').innerHTML = timeHtml(health.data_freshness?.steps);
  setState('database-state', health.database === 'healthy' ? connection.running : connection.unreachable, health.database === 'healthy');
  setState('scheduler-state', schedulerText(health.scheduler), health.scheduler === 'running');
  $('webhook-info').innerHTML = writeHtml(health.latest_health_write);
  $('queue-state').textContent = connection.queue(
    num(health.pending_notifications ?? 0),
    num((health.failed_notifications ?? 0) + (health.failed_syncs ?? 0)),
  );
}

/** The backup row, from the reasons /health gives. A 503 report has no field at all. @param {Health} health */
function backupDetail(health) {
  const reasons = health.reasons ?? [];
  const success = health.last_backup_success;
  if (reasons.includes('backup_missing')) return system.backup.missing;
  if (reasons.includes('backup_status_unreadable')) return system.backup.unreadable;
  if (reasons.includes('backup_failed')) return success ? system.backup.failedSince(shortMoment(success)) : system.backup.failed;
  if (success === undefined) return '—';
  return success ? shortMoment(success) : system.backup.notTracked;
}

/** @param {Health} health @param {ReturnType<typeof serviceView>} service @returns {[string, string][]} */
function systemDetails(health, service) {
  return [
    [system.details.service, service.detail],
    ...sourceDetails(health),
    [system.details.lastCheck, shortMoment(health.checked_at)],
    [system.details.lastWrite, shortMoment(health.latest_health_write)],
    [system.details.backup, backupDetail(health)],
    [system.details.notificationErrors, num(health.failed_notifications ?? 0)],
    [system.details.syncErrors, num(health.failed_syncs ?? 0)],
    [system.details.staleTypes, num(health.stale_streams?.length ?? 0)],
  ];
}

/** "Şimdi eşitle" starts a Google sync; without Google it is off and the note says why. @param {boolean} google */
function explainSyncButton(google) {
  const button = $('sync-button');
  button.title = google ? '' : system.garmin.syncUnavailable;
  if (google) button.removeAttribute('aria-describedby');
  else button.setAttribute('aria-describedby', 'webhook-info');
}

/** @typedef {ReturnType<typeof garminPull>} GarminPull */
/** Footer words for the pull; the footer stays on every screen, also where the note gives its height to the charts. @param {GarminPull} pull */
const pullLine = pull => `Garmin: ${pull.problem ? pull.state : pull.last || pull.state}`;
/** @param {GarminPull} pull */
const problemText = pull => (pull.problem ? system.garmin.problem(pull.state, pull.last) : '');

/** Beside Google: only a pull problem is added, after Google's queue and before its last write. @param {Health} health @param {GarminPull} pull */
function paintGarminBesideGoogle(health, pull) {
  if (!pull.problem) return;
  $('queue-state').textContent += ` · ${pullLine(pull)}`;
  $('webhook-info').innerHTML = `${escape(problemText(pull))} ${writeHtml(health.latest_health_write)}`;
}

/** Garmin alone: the pull replaces Google's queue, last sync and note. @param {Health} health @param {GarminPull} pull */
function paintGarminAlone(health, pull) {
  $('queue-state').textContent = pullLine(pull);
  $('last-sync').innerHTML = timeHtml(health.garmin?.last_success_at);
  $('webhook-info').innerHTML = garminNoteHtml(pull);
}

/** @param {GarminPull} pull */
function garminNoteHtml(pull) {
  const lead = pull.problem ? `${problemText(pull)} ${system.garmin.autoPull}` : system.garmin.autoPull;
  return `${escape(lead)}${!pull.problem && pull.last ? `<small>${escape(pull.last)}</small>` : ''}`;
}

/**
 * Garmin on the overview: its pull problem is named, and without Google the note says why
 * "Şimdi eşitle" is off (Garmin is pulled on its own; the button starts a Google sync).
 * @param {Health} health
 */
function paintGarmin(health) {
  const google = sourceEnabled(health, 'google');
  explainSyncButton(google);
  if (!sourceEnabled(health, 'garmin')) return;
  if (google) paintGarminBesideGoogle(health, garminPull(health));
  else paintGarminAlone(health, garminPull(health));
}

/** @param {Health} health */
export function paintHealth(health) {
  paintLoginWarning(health);
  const service = serviceView(health.status);
  paintConnectionPanel(health, service);
  paintSources(health);
  paintGarmin(health);
  $('system-details').innerHTML = systemDetailsHtml(health, service);
}

/** @param {Health} health @param {ReturnType<typeof serviceView>} service */
function systemDetailsHtml(health, service) {
  return systemDetails(health, service)
    .map(([label, value]) => `<div><dt>${escape(label)}</dt><dd>${escape(value)}</dd></div>`)
    .join('');
}

/** /health did not answer: every "checking" text of the panel says so instead of waiting. */
export function paintHealthFailed() {
  // Nothing can start a sync while the service does not answer; the status beside it says why.
  const button = $('sync-button');
  button.disabled = true;
  button.title = connection.serviceUnreachable;
  button.setAttribute('aria-describedby', 'service-status');
  $('service-status').textContent = connection.serviceUnreachable;
  $('service-status').className = 'status-text bad';
  $('webhook-info').textContent = connection.statusUnknown;
  $('system-details').textContent = system.unreachable;
}
