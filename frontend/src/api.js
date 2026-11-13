// Shared fetch helpers for the app's own endpoints. Every failure is an ApiError whose `kind` says
// what went wrong, so no caller can mistake a failed answer for an empty archive.
import { language, t } from './i18n/index.js';
import { SERVER_CODES, SERVER_MESSAGES, SERVER_PATTERNS } from './i18n/server-messages.js';

/** @typedef {'network'|'timeout'|'unauthorized'|'server'|'rejected'|'malformed'} ApiFailure */

export class ApiError extends Error {
  /** @param {string} message @param {{kind: ApiFailure, status?: number, cause?: unknown}} details */
  constructor(message, { kind, status, cause }) {
    super(message, cause === undefined ? undefined : { cause });
    this.name = 'ApiError';
    this.kind = kind;
    this.status = status;
  }
}

/**
 * Text for a caught failure. Only ApiError carries words meant for the user.
 * @param {unknown} error
 */
export const errorText = error => (error instanceof TypeError ||
  !(/** @type {{message?: string} | null} */ (error))?.message ? t.common.api.failed : /** @type {{message: string}} */ (error).message);

const TIMEOUT_MESSAGE = t.common.api.timeout;
const UNREACHABLE_MESSAGE = t.common.api.unreachable;

let redirecting = false;
/** Sends the browser to the login page and brings it back to the current page afterwards. */
export function redirectToLogin() {
  if (redirecting) return;
  redirecting = true;
  location.assign(`/login?${new URLSearchParams({ next: `${location.pathname}${location.search}${location.hash}` })}`);
}

/**
 * Returns true (and redirects) when the server asks for a login.
 * @param {Response} response
 */
export function handleUnauthorized(response) {
  if (response.status !== 401) return false;
  redirectToLogin();
  return true;
}

/** @param {unknown} error */
const isTimeout = error => (/** @type {{name?: string} | null} */ (error))?.name === 'TimeoutError' || (/** @type {{name?: string} | null} */ (error))?.name === 'AbortError';

/**
 * The user-facing message for a fetch that failed before a usable response arrived.
 * @param {unknown} error
 */
export function failureMessage(error) {
  return isTimeout(error) ? TIMEOUT_MESSAGE : UNREACHABLE_MESSAGE;
}

/**
 * fetch, with its failures as ApiError and the 401 redirect done.
 * @param {string} url @param {RequestInit} options @param {number} timeout
 */
async function send(url, options, timeout) {
  const expiry = AbortSignal.timeout(timeout);
  const signal = options.signal ? AbortSignal.any([options.signal, expiry]) : expiry;
  let response;
  try {
    response = await fetch(url, { ...options, cache: 'no-store', signal });
  } catch (error) {
    throw new ApiError(failureMessage(error), { kind: isTimeout(error) ? 'timeout' : 'network', cause: error });
  }
  if (handleUnauthorized(response)) throw new ApiError(t.common.api.sessionExpired, { kind: 'unauthorized', status: 401 });
  return response;
}

/**
 * The JSON body; a proxy error page (an HTML 502) or a body cut off by the timeout is a failure.
 * @param {Response} response
 */
async function readJson(response) {
  const { status } = response;
  try {
    return await response.json();
  } catch (error) {
    if (isTimeout(error)) throw new ApiError(TIMEOUT_MESSAGE, { kind: 'timeout', status, cause: error });
    throw new ApiError(response.ok ? t.common.api.unexpectedResponse : UNREACHABLE_MESSAGE, { kind: 'malformed', status, cause: error });
  }
}

/** @param {number} status @returns {ApiFailure} */
const statusKind = status => (status >= 500 ? 'server' : 'rejected');

/** @type {Record<number, string>} */
const BY_STATUS = { 400: 'invalid', 403: 'forbidden', 404: 'notFound', 409: 'conflict', 413: 'tooLarge', 422: 'invalid', 429: 'rateLimited' };
/** @param {number} status */
const statusText = status => /** @type {Record<string, string>} */ (t.common.api.refused)[status >= 500 ? 'server' : BY_STATUS[status]] ?? t.common.api.failed;

// Turkish letters mark a Turkish sentence; the server writes in Turkish or English.
const TURKISH = /[çğıöşüÇĞİÖŞÜ]/u;
/** @param {string} text @param {string} lang */
const inLanguage = (text, lang) => TURKISH.test(text) === (lang === 'tr');

/** The dictionary's words for a recognised server message. @param {string | undefined} key @param {string[]} values */
function knownText(key, ...values) {
  const entry = key ? /** @type {Record<string, string | ((...values: string[]) => string)>} */ (t.common.api.known)[key] : undefined;
  return typeof entry === 'function' ? entry(...values) : entry;
}

/** A server sentence in the interface's words, or undefined. @param {string} detail @param {string} lang */
function sentenceText(detail, lang) {
  for (const [pattern, key] of SERVER_PATTERNS) {
    const match = pattern.exec(detail);
    if (match) return knownText(key, ...match.slice(1));
  }
  const known = knownText(SERVER_MESSAGES[detail]);
  if (known) return known;
  return detail.trim() && inLanguage(detail, lang) ? detail : undefined;
}

/**
 * Words for a refused request in the interface's language. A code or a sentence this interface
 * knows is said in its own words; another sentence is shown only when it is already in the
 * interface's language; everything else gets a text chosen by the HTTP status.
 * @param {number} status @param {unknown} detail @param {string} [lang] the interface's language
 */
export function refusedText(status, detail, lang = language) {
  const text = typeof (/** @type {{code?: unknown} | null} */ (detail))?.code === 'string' ?
    knownText(SERVER_CODES[/** @type {{code: string}} */ (detail).code], String((/** @type {{max_days?: unknown}} */ (detail)).max_days ?? ''))
    : typeof detail === 'string' ? sentenceText(detail, lang) : undefined;
  return text ?? statusText(status);
}

/**
 * /health answers 503 with a report of the service (it names the database's state) when the
 * database is down; every other non-2xx (a 500 with an error body, a proxy's JSON) is a failed
 * request, not a description of the service.
 * @param {Partial<import('./state.js').Health> | null} payload @param {Response} response
 */
function isHealthReport(payload, response) {
  if (!payload || typeof payload.status !== 'string') return false;
  return response.ok || (response.status === 503 && typeof payload.database === 'string');
}

/** Service status. Rejects unless the answer really is a health report. */
export async function fetchHealth() {
  const response = await send('/health', {}, 15000);
  const payload = await readJson(response);
  if (isHealthReport(payload, response)) return payload;
  const { status } = response;
  throw new ApiError(t.common.api.failed, { kind: response.ok ? 'malformed' : statusKind(status), status });
}

/**
 * The endpoint's JSON. The caller names the shape it asked for (api-types.d.ts, or a typedef beside
 * it); without one the answer is `unknown`, so an unchecked field access does not compile.
 * @template [T=unknown]
 * @param {string} url
 * @param {RequestInit} [options]
 * @returns {Promise<T>}
 */
export async function request(url, options = {}) {
  const response = await send(url, options, 20000);
  const payload = await readJson(response);
  if (response.ok) return payload;
  throw new ApiError(refusedText(response.status, payload?.detail), { kind: statusKind(response.status), status: response.status });
}
