// Apple Health import panel on the System page: upload the Health app's export.zip (or a bare
// export.xml) as the raw request body, follow the upload and then the server's progress, and
// show the Health Auto Export receiver's state. One import at a time; the panel stays usable
// on every viewport (long messages scroll inside the panel). GitHub-only Apple extension.
// It imports no module of the initial bundle: the System page hands it the helpers it needs
// (`setupAppleImport`), so the bundler never splits a shared chunk out of the initial code.
import { en, tr } from './i18n/apple.js';

/**
 * @typedef {{
 *   request: typeof import('./api.js').request,
 *   handleUnauthorized: typeof import('./api.js').handleUnauthorized,
 *   number: typeof import('./locale.js').number,
 *   dateFormat: typeof import('./locale.js').dateFormat,
 *   getZone: () => string,
 * }} AppleTools
 */
/** @type {AppleTools} */
let tools;
/** @type {typeof tr} */
let strings = tr;
/** @param {string} id @returns {HTMLElement} */
const $ = id => /** @type {HTMLElement} */ (document.getElementById(id));

const IMPORT_URL = '/api/apple/import';
const STATUS_URL = '/api/apple/status';
const POLL_MS = 1000;
const MB = 1024 * 1024;

/** @param {number | null | undefined} value */
const count = value => tools.number(value ?? 0);
/** @param {number} value */
const megabytes = value => tools.number(value / MB, value < 10 * MB ? 1 : 0);

/** @typedef {{records_read?: number, records_new?: number}} Progress */
/** @typedef {{enabled?: boolean, token_file_present?: boolean}} Receiver */
/** @typedef {Progress & {channel?: string, status?: string, error?: string | null, started_at?: string, finished_at?: string | null}} ImportRow */
/** @typedef {{receiver?: Receiver | null, running?: Progress | null, imports?: ImportRow[]}} AppleStatus */
/** @typedef {Progress & {export_date?: string | null, summary?: {skipped?: unknown, export_date?: string | null}}} ImportResult */
/** @typedef {(value: string | null | undefined) => string | null} DayFormat */

/** @param {File} file */
function contentType(file) {
  const name = file.name.toLowerCase();
  if (name.endsWith('.zip')) return 'application/zip';
  if (name.endsWith('.xml')) return 'application/xml';
  return 'application/octet-stream';
}

/**
 * Total of a counter object ({Me: 1, ClinicalRecord: 3}) or a plain number.
 * @param {unknown} value @returns {number}
 */
function total(value) {
  if (typeof value === 'number') return value;
  if (value && typeof value === 'object') return Object.values(value).reduce((sum, item) => sum + total(item), 0);
  return 0;
}

/** @param {Receiver | null | undefined} receiver */
function receiverText(receiver) {
  if (!receiver) return '';
  if (!receiver.enabled) return strings.receiverOff;
  return receiver.token_file_present ? strings.receiverOn : strings.receiverNoToken;
}

/** @param {ImportResult} result @param {DayFormat} day */
function summary(result, day) {
  const parts = [strings.recordsRead(count(result.records_read)), strings.recordsNew(count(result.records_new))];
  const skipped = total(result.summary?.skipped);
  if (skipped) parts.push(strings.skipped(count(skipped)));
  const exported = day(result.export_date ?? result.summary?.export_date);
  if (exported) parts.push(strings.exportDate(exported));
  return strings.done(parts.join(' · '));
}

/** @param {{loaded: number, total: number, done: boolean}} uploaded @param {Progress | null | undefined} running */
function progressText(uploaded, running) {
  const parts = [];
  if (uploaded.total && !uploaded.done) {
    const percent = Math.floor((uploaded.loaded / uploaded.total) * 100);
    parts.push(strings.uploading(percent, megabytes(uploaded.loaded), megabytes(uploaded.total)));
  } else if (uploaded.done) {
    parts.push(strings.sent);
  }
  if (running) parts.push(strings.progressRecords(count(running.records_read), count(running.records_new)));
  return parts.length ? `${parts.join(' · ')}…` : strings.starting;
}

/** @param {XMLHttpRequest} xhr */
function failureText(xhr) {
  const body = xhr.response;
  const detail = typeof body?.detail === 'string' ? body.detail : null;
  const fallback = /** @type {Record<number, string>} */ ({
    409: strings.conflict,
    413: strings.tooLarge,
    415: strings.wrongType,
  })[xhr.status];
  return strings.failed(detail ?? fallback ?? strings.serverFailed);
}

/**
 * The newest export import's line: [text, warning].
 * @param {ImportRow[] | undefined} imports @param {DayFormat} day @returns {[string, boolean]}
 */
function lastImportText(imports, day) {
  const last = imports?.find(item => item.channel === 'export');
  if (last?.status === 'done') {
    return [strings.last(day(last.finished_at ?? last.started_at) ?? '—', count(last.records_read), count(last.records_new)), false];
  }
  if (last?.status === 'failed') return [strings.lastFailed(last.error ?? strings.unknownError), true];
  return ['', false];
}

/** The panel's elements and the state of its one import. */
function panelState() {
  return {
    file: /** @type {HTMLInputElement} */ ($('apple-import-file')),
    start: /** @type {HTMLButtonElement} */ ($('apple-import-start')),
    bar: /** @type {HTMLProgressElement} */ ($('apple-import-progress')),
    status: $('apple-import-status'),
    busy: false,
    /** @type {ReturnType<typeof setInterval> | null} */
    timer: null,
    polling: false,
    uploaded: { loaded: 0, total: 0, done: false },
  };
}
/** @typedef {ReturnType<typeof panelState>} Panel */

/** @param {Panel} panel @param {string} text @param {boolean} [warning] */
function say(panel, text, warning = false) {
  panel.status.textContent = text;
  panel.status.classList.toggle('warning', warning);
}

/** @param {Panel} panel @param {boolean} value */
function setBusy(panel, value) {
  panel.busy = value;
  panel.file.disabled = value;
  panel.start.disabled = value || !panel.file.files?.length;
  panel.start.setAttribute('aria-busy', String(value));
  $('apple-import-panel').setAttribute('aria-busy', String(value));
}

/** @param {Panel} panel */
function stopPolling(panel) {
  if (panel.timer !== null) clearInterval(panel.timer);
  panel.timer = null;
}

/** @param {Panel} panel @param {() => Promise<AppleStatus>} status */
async function poll(panel, status) {
  if (panel.polling) return;
  panel.polling = true;
  try {
    const current = await status();
    $('apple-import-receiver').textContent = receiverText(current.receiver);
    if (panel.busy) say(panel, progressText(panel.uploaded, current.running));
  } catch {
    // A missed poll is harmless; the upload's own answer ends the import.
  } finally {
    panel.polling = false;
  }
}

/** @param {Panel} panel @param {XMLHttpRequest} xhr */
function followUpload(panel, xhr) {
  xhr.upload.onprogress = event => {
    if (!event.lengthComputable) return;
    panel.uploaded.loaded = event.loaded;
    panel.uploaded.total = event.total;
    panel.bar.value = Math.floor((event.loaded / event.total) * 100);
    say(panel, progressText(panel.uploaded, null));
  };
  xhr.upload.onload = () => {
    panel.uploaded.done = true;
    panel.bar.removeAttribute('value'); // Indeterminate while the server finishes.
    say(panel, progressText(panel.uploaded, null));
  };
}

/**
 * Sends the chosen file and reports the outcome; the status is polled meanwhile.
 * @param {Panel} panel @param {() => Promise<AppleStatus>} status @param {DayFormat} day
 */
function upload(panel, status, day) {
  const chosen = panel.file.files?.[0];
  if (!chosen || panel.busy) return;
  setBusy(panel, true);
  panel.uploaded = { loaded: 0, total: chosen.size, done: false };
  panel.bar.hidden = false;
  panel.bar.value = 0;
  panel.bar.max = 100;
  say(panel, progressText(panel.uploaded, null));
  const xhr = new XMLHttpRequest();
  xhr.open('POST', IMPORT_URL);
  xhr.responseType = 'json';
  xhr.setRequestHeader('Content-Type', contentType(chosen));
  xhr.setRequestHeader('X-HealthOS-Request', '1');
  xhr.setRequestHeader('X-Apple-Export-Filename', encodeURIComponent(chosen.name));
  followUpload(panel, xhr);
  /** @param {string} text @param {boolean} warning */
  const finish = (text, warning) => {
    stopPolling(panel);
    panel.bar.hidden = true;
    setBusy(panel, false);
    say(panel, text, warning);
  };
  xhr.onload = () => {
    // handleUnauthorized reads only the status, which an XMLHttpRequest has too.
    if (tools.handleUnauthorized(/** @type {Response} */ (/** @type {unknown} */ (xhr)))) { finish(strings.sessionExpired, true); return; }
    if (xhr.status >= 200 && xhr.status < 300 && xhr.response) {
      panel.file.value = '';
      finish(summary(xhr.response, day), false);
      return;
    }
    finish(failureText(xhr), true);
  };
  xhr.onerror = () => finish(strings.unreachable, true);
  xhr.onabort = () => finish(strings.aborted, true);
  panel.timer = setInterval(() => poll(panel, status), POLL_MS);
  xhr.send(chosen);
}

/** @param {Panel} panel @param {() => Promise<AppleStatus>} status @param {DayFormat} day */
async function refresh(panel, status, day) {
  if (panel.busy) return;
  try {
    const current = await status();
    if (panel.busy) return;
    $('apple-import-receiver').textContent = receiverText(current.receiver);
    if (current.running) {
      say(panel, strings.running(count(current.running.records_read), count(current.running.records_new)));
      panel.start.disabled = true;
      return;
    }
    panel.start.disabled = !panel.file.files?.length;
    if (!panel.status.textContent || panel.status.dataset.source === 'status') {
      panel.status.dataset.source = 'status';
      const [text, warning] = lastImportText(current.imports, day);
      say(panel, text, warning);
    }
  } catch {
    $('apple-import-receiver').textContent = strings.statusFailed;
  }
}

/**
 * @param {AppleTools} given
 * @returns {{refresh: () => Promise<void>}}
 */
export function setupAppleImport(given) {
  tools = given;
  // The page's language, which i18n/index.js sets on <html> before any module renders.
  strings = document.documentElement.lang === 'en' ? en : tr;
  const { request, getZone } = given;
  const panel = panelState();
  /** @type {DayFormat} */
  // The long date ("16 Ekim 2026"), as dates.js fullDate writes it; that module stays in its own chunk.
  const day = value =>
    value ? tools.dateFormat({ timeZone: getZone(), day: 'numeric', month: 'long', year: 'numeric' }).format(new Date(value)) : null;
  const status = () => /** @type {Promise<AppleStatus>} */ (request(STATUS_URL));
  panel.file.addEventListener('change', () => { panel.start.disabled = panel.busy || !panel.file.files?.length; });
  panel.start.addEventListener('click', () => {
    panel.status.dataset.source = 'upload';
    upload(panel, status, day);
  });
  window.addEventListener('pagehide', () => stopPolling(panel));
  return { refresh: () => refresh(panel, status, day) };
}
