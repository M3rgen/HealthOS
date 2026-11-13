// Text formatters shared by every view that builds markup: escape() for any text placed in HTML,
// num() and percent() for numbers (interface-language grouping, "—" when missing), duration()
// for seconds. Dates live in dates.js. These two are the only sources of those formats.
import { t } from './i18n/index.js';
import { number } from './locale.js';

/** @param {unknown} value */
export const escape = value =>
  String(value ?? '').replace(
    /[&<>"']/g,
    c => /** @type {Record<string, string>} */ ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c],
  );
/** @param {unknown} value @param {number} [digits] */
export const num = (value, digits = 0) => (value == null ? '—' : number(/** @type {number|string} */ (value), digits));
/** "%17,8" in Turkish, "17.8%" in English: the sign's place comes from the dictionary. @param {number|string|null|undefined} value @param {number} [digits] */
export const percent = (value, digits = 1) => (value == null ? '—' : t.common.percent(number(/** @type {number|string} */ (value), digits)));
// "45 dk", not "0 sa 45 dk": the hour part is shown only when there is one.
/** @param {number|null|undefined} seconds */
export const duration = seconds => {
  if (seconds == null) return '—';
  const minutes = Math.round(seconds / 60);
  return t.common.duration(Math.floor(minutes / 60), minutes % 60);
};
