// "2 saat önce": a coarse distance from `now`, for a time whose exact value is shown alongside
// (a tooltip, a details list). Future instants (a clock ahead of the server) read as "az önce"
// rather than a negative age. The wording comes from t.common.relativeTime.
import { t } from './i18n/index.js';

/** @type {[number, 'minutes' | 'hours' | 'days' | 'weeks' | 'months' | 'years', number][]} */
const STEPS = [
  [60, 'minutes', 60],
  [24, 'hours', 3600],
  [7, 'days', 86400],
  [5, 'weeks', 604800],
  [12, 'months', 2629800],
  [Infinity, 'years', 31557600],
];

/** @param {string | number | Date | null | undefined} value @param {string | number | Date} [now] */
export function relativeTime(value, now = new Date()) {
  if (!value) return null;
  const at = new Date(value);
  if (Number.isNaN(at.getTime())) return null;
  const words = t.common.relativeTime;
  const seconds = Math.max(0, Math.floor((new Date(now).getTime() - at.getTime()) / 1000));
  if (seconds < 60) return words.justNow;
  for (const [limit, unit, size] of STEPS) {
    const count = Math.floor(seconds / size);
    if (count < limit) return words[unit](count);
  }
  return null;
}
