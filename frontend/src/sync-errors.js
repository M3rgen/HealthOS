// Readable wording for the short error codes in /sync/status `last_error`
// (e.g. "GoogleHealthError (http_429)", "TimeoutException", "process restarted").
import { t } from './i18n/index.js';

// "ErrorClass" or "ErrorClass (short_code)".
const CODE = /^[A-Za-z_][\w.]*(?: \([\w.:-]+\))?$/;

/** The wording for an HTTP status in the code, if it has one of its own. @param {string} raw */
function httpStatusWording(raw) {
  const words = t.common.syncErrors;
  const status = Number(/\bhttp_(\d{3})\b/i.exec(raw)?.[1]);
  if (status === 401 || status === 403) return words.accessDenied;
  if (status === 429) return words.rateLimited;
  if (status >= 500 && status <= 599) return words.serverError;
  return null;
}

/** @param {string|null|undefined} code */
export function describeSyncError(code) {
  const words = t.common.syncErrors;
  const raw = String(code ?? '').trim();
  if (!raw) return words.fallback;
  const wording = httpStatusWording(raw);
  if (wording !== null) return wording;
  if (/timeout/i.test(raw)) return words.timeout;
  if (/^process restarted$/i.test(raw)) return words.restarted;
  // Older rows may still hold a readable sentence instead of a code; show those unchanged.
  if (!CODE.test(raw)) return raw;
  // Unknown code: keep it visible so it can still be looked up.
  return words.unknown(raw);
}

/** The raw "type: code" lines kept in a notice's tooltip for debugging. @param {[string, string|null|undefined][]} pairs */
export function rawErrors(pairs) {
  return pairs
    .filter(([, code]) => code)
    .map(([type, code]) => `${type}: ${code}`)
    .join('\n');
}
