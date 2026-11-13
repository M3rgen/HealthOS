// Every date and time the interface shows, in one place. "Civil" dates are the API's yyyy-mm-dd
// day keys: they are read at noon UTC and always formatted in UTC, so no time zone can shift the
// day. Instants (ISO timestamps) are formatted in the time zone the caller names.
import { dateFormat } from './locale.js';

/** A civil yyyy-mm-dd key as a Date at noon UTC. @param {string|number|Date} value */
export const civilDate = value => new Date(`${value}T12:00:00Z`);

/** The yyyy-mm-dd key of an instant in `zone` (a technical format: en-CA for its digit order). @param {string|number|Date} value @param {string} zone */
export function dateKey(value, zone) {
  const parts = new Intl.DateTimeFormat('en-CA', { timeZone: zone, year: 'numeric', month: '2-digit', day: '2-digit' })
    .formatToParts(new Date(value));
  const part = (/** @type {Intl.DateTimeFormatPartTypes} */ type) => parts.find(item => item.type === type)?.value ?? '';
  return `${part('year')}-${part('month')}-${part('day')}`;
}
/** Today's yyyy-mm-dd key in `zone`. @param {string} zone */
export const todayKey = zone => dateKey(new Date(), zone);

/** An instant as a datetime-local wall clock in `zone`. @param {string|number|Date} instant @param {string} zone */
export function zonedInputValue(instant, zone) {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: zone, year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  }).formatToParts(new Date(instant));
  const part = (/** @type {Intl.DateTimeFormatPartTypes} */ type) => parts.find(item => item.type === type)?.value ?? '';
  return `${part('year').padStart(4, '0')}-${part('month')}-${part('day')}T${part('hour')}:${part('minute')}`;
}

/** A datetime-local wall clock interpreted in `zone`, independent of the device. @param {string} value @param {string} zone */
export function zonedInstant(value, zone) {
  const wall = Date.parse(`${value}Z`);
  if (!Number.isFinite(wall)) return new Date(NaN);
  // Sample both sides of a possible offset transition, including a whole skipped day.
  const offsets = [-1, 0, 1].map(day => {
    const sample = wall + day * 86400000;
    return Date.parse(`${zonedInputValue(sample, zone)}Z`) - sample;
  });
  const candidates = [...new Set(offsets)].map(offset => wall - offset).sort((a, b) => a - b);
  // Temporal's "compatible" rule: an overlap chooses the earlier instant; a gap moves the
  // wall clock forward by the gap's length (using the offset from before the transition).
  const exact = candidates.find(candidate => zonedInputValue(candidate, zone) === value);
  return new Date(exact ?? candidates.find(candidate => zonedInputValue(candidate, zone) > value) ?? NaN);
}
/** The day `days` after a civil key (negative for earlier days). @param {string|number|Date} value @param {number} days */
export function shiftDay(value, days) {
  const date = civilDate(value);
  date.setUTCDate(date.getUTCDate() + days);
  return date.toISOString().slice(0, 10);
}

/** @param {string|number|Date} value @param {string} zone */
export const shortDate = (value, zone) => dateFormat({ timeZone: zone, day: 'numeric', month: 'short' }).format(new Date(value));
/** @param {string|number|Date} value @param {string} zone */
export const fullDate = (value, zone) =>
  dateFormat({ timeZone: zone, day: 'numeric', month: 'long', year: 'numeric' }).format(new Date(value));
/** @param {string|number|Date} value @param {string} zone */
export const clockTime = (value, zone) => dateFormat({ timeZone: zone, hour: '2-digit', minute: '2-digit' }).format(new Date(value));
/** "16 Ekim 2026 15:58": the long form of an instant, wherever one is shown in full. @param {string|number|Date} value @param {string} zone */
export const fullDateTime = (value, zone) => dateFormat({ timeZone: zone, dateStyle: 'long', timeStyle: 'short' }).format(new Date(value));

/** "16 Eki" for a civil key. @param {string|number|Date} value */
export const civilShort = value => shortDate(civilDate(value), 'UTC');
/** "16 Ekim 2026" for a civil key. @param {string|number|Date} value */
export const civilFull = value => fullDate(civilDate(value), 'UTC');
/** "9 Eki – 15 Ekim 2026" for two civil keys. @param {string} start @param {string} end */
export function civilRange(start, end) {
  const first = start.slice(0, 4) === end.slice(0, 4)
    ? civilShort(start)
    : civilParts(start, { day: 'numeric', month: 'short', year: 'numeric' });
  return `${first} – ${civilFull(end)}`;
}
/** A civil key shown with its own pattern (weekday, month and year names). @param {string|number|Date} value @param {Intl.DateTimeFormatOptions} options */
export const civilParts = (value, options) => dateFormat({ timeZone: 'UTC', ...options }).format(civilDate(value));
