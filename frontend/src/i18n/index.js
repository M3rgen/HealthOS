// Interface language. Turkish ships with the page: Vite fills index.html from tr.js at build/dev time.
// English (en.js) is fetched with import() only when it is wanted, and before any module renders
// text: every module reads its strings through `t` from here, and this module's top-level await
// finishes before theirs run. A choice made in Sistem › Görünüm wins and lives in this browser
// only; without one the browser's own language decides (navigator.languages).
import { stored } from '../local-store.js';
import { tr } from './tr.js';

const KEY = 'healthos.language';
export const LANGUAGES = ['tr', 'en'];
const chosen = stored(KEY) ?? null;

/**
 * The browser's first preferred language this interface has, by primary subtag (en-GB is en).
 * A browser that names neither gets English, the one a stranger is likelier to read. Without a
 * browser it is Turkish, the language the page ships in: Node's own `navigator` (unit tests,
 * tools) names the machine's locale, not a reader's.
 * @param {{language?: string, languages?: readonly string[], userAgent?: string} | undefined} browser
 */
export function browserLanguage(browser) {
  if (!browser || browser.userAgent?.startsWith('Node.js')) return 'tr';
  const preferred = browser.languages?.length ? browser.languages : [browser.language ?? ''];
  const found = preferred.map(tag => String(tag).toLowerCase().split('-')[0]).find(code => LANGUAGES.includes(code));
  return found ?? 'en';
}
const wanted = chosen !== null && LANGUAGES.includes(chosen) ? chosen : browserLanguage(globalThis.navigator);

// en.js is complete (tests/frontend/i18n-completeness.test.mjs). A gap that still reaches a browser
// shows the Turkish text rather than breaking the page, and is reported instead of hidden.
/**
 * @template {Record<string, unknown>} T
 * @param {T} base @param {Record<string, unknown> | null | undefined} over @param {(where: string) => void} [report] @param {string} [path]
 * @returns {T} the same shape as `base`, with each string or function taken from `over` when present
 */
export function merge(base, over, report = () => {}, path = '') {
  /** @type {Record<string, unknown>} */
  const out = {};
  for (const [key, value] of Object.entries(base)) {
    const where = path ? `${path}.${key}` : key;
    if (value && typeof value === 'object') out[key] = merge(/** @type {Record<string, unknown>} */ (value), /** @type {Record<string, unknown> | undefined} */ (over?.[key]), report, where);
    else if (over?.[key] != null) out[key] = over[key];
    else {
      report(where);
      out[key] = value;
    }
  }
  return /** @type {T} */ (out);
}
/** Prints gaps at once, so a missing translation is visible in the console, not silent. @param {string} where */
export const reportMissing = where => console.warn(`i18n: no English text for ${where}`);

// A chunk that cannot be fetched (network, a tab left open across an update) must not stop the
// page: it opens in Turkish, the choice stays stored, and main.js says so with a retry.
const english = wanted === 'en' ? await import('./en.js').catch(() => null) : null;
/** The language of the texts on screen: the chosen one, or Turkish if its strings did not load. */
export const language = english ? 'en' : 'tr';
/** The chosen language could not be loaded and Turkish is shown instead. */
export const languageFallback = wanted !== language;
/** The interface strings in the chosen language. */
export const t = english ? merge(tr, english.en, reportMissing) : tr;

// Static markup: data-i18n names the dictionary key of an element's own text (its first text
// node, so child elements such as icons stay); data-i18n-<attribute> names an attribute's key.
const ATTRIBUTES = ['aria-label', 'title', 'placeholder'];
/** @param {Record<string, string>} page @param {ParentNode} [root] @param {(key: string) => void} [report] */
export function translatePage(page, root = document, report = reportMissing) {
  for (const element of /** @type {NodeListOf<HTMLElement>} */ (root.querySelectorAll('[data-i18n]'))) {
    const text = page[/** @type {string} */ (element.dataset.i18n)];
    if (text == null) {
      report(element.dataset.i18n ?? '');
      continue;
    }
    const node = /** @type {Text | undefined} */ ([...element.childNodes].find(child => child.nodeType === Node.TEXT_NODE && child.textContent?.trim()));
    if (node) node.data = node.data.replace(node.data.trim(), text);
    else element.textContent = text;
  }
  for (const attribute of ATTRIBUTES)
    for (const element of root.querySelectorAll(`[data-i18n-${attribute}]`)) {
      const key = /** @type {string} */ (element.getAttribute(`data-i18n-${attribute}`));
      const text = page[key];
      if (text != null) element.setAttribute(attribute, text);
      else report(key);
    }
}
if (english && typeof document !== 'undefined') {
  document.documentElement.lang = language;
  translatePage(english.page);
  if (english.page['document.title']) document.title = english.page['document.title'];
}

/**
 * Stores the choice, Turkish too (it must outlast an English browser), and reloads when the page
 * is not already in it, so every module renders again in the new language.
 * @param {string} value
 */
export function setLanguage(value) {
  if (!LANGUAGES.includes(value) || value === chosen) return;
  stored(KEY, value);
  if (value !== language) location.reload();
}
