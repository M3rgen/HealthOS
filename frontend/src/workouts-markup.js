// Sports page markup: one list of days, each with its sessions, overlapping sessions from
// lower-ranked sources folded under the session they overlap.
import { exerciseLabel, sourceLabel } from './data-types.js';
import { dateKey } from './dates.js';
import { t } from './i18n/index.js';

const text = t.workouts;

/**
 * @typedef {{id: string | number, source: string, source_code?: string, exercise_type: string, start_time: string, end_time: string,
 *   duration_seconds: number | null, calories_kcal: number | null, distance_meters: number | null, priority?: string,
 *   group?: string | number | null, alternatives?: unknown[]}} WorkoutRow
 * @typedef {{escape: (value: unknown) => string, num: (value: number | null | undefined, digits?: number) => string,
 *   duration: (seconds: number | null | undefined) => string, fullDate: (value: string) => string, timeOf: (value: string) => string}} WorkoutFormat
 */

/** @param {WorkoutRow} row */
const secondary = row => row.priority === 'secondary';
/** @param {WorkoutRow} row */
const titleOf = row => exerciseLabel(row.exercise_type) || text.untitled;
/** @param {WorkoutFormat} format @param {WorkoutRow} row */
const keyAttributes = ({ escape }, row) =>
  `data-source="${escape(row.source)}" data-workout-id="${escape(row.id)}" data-key="${escape(`${row.source}:${row.id}`)}"`;

/** Source and priority travel as text badges: the colour alone never carries the meaning. @param {WorkoutFormat} format @param {WorkoutRow} row @param {WorkoutRow[]} others */
function badges({ escape }, row, others) {
  // 'Manuel' is the source value the API sends for hand-entered rows (data); the badge text comes from `t`.
  const manual = row.source === 'Manuel' || row.source_code === 'MANUAL';
  const source = `<span class="workout-source ${manual ? 'workout-manual' : 'workout-device'}">${escape(manual ? text.manual : sourceLabel(row.source_code, row.source))}</span>`;
  const priority = secondary(row) ? `<span class="workout-priority workout-secondary">${escape(text.secondary)}</span>`
    : (row.alternatives?.length || others.length) ? `<span class="workout-priority">${escape(text.primary)}</span>` : '';
  return source + priority;
}

/** @param {WorkoutFormat} format @param {string} value */
const time = ({ escape, timeOf }, value) => `<time datetime="${escape(value)}">${escape(timeOf(value))}</time>`;

/** @param {WorkoutFormat} format @param {WorkoutRow} row */
function values({ escape, num, duration }, row) {
  return `<dl class="workout-values">
        <div><dt>${escape(text.duration)}</dt><dd>${duration(row.duration_seconds)}</dd></div>
        <div><dt>${escape(text.calories)}</dt><dd>${num(row.calories_kcal,1)}${row.calories_kcal == null ? '' : ' kcal'}</dd></div>
        <div><dt>${escape(text.distance)}</dt><dd>${num(row.distance_meters == null ? null : row.distance_meters/1000,2)}${row.distance_meters == null ? '' : ' km'}</dd></div>
      </dl>`;
}

/**
 * @param {WorkoutFormat} format @param {WorkoutRow} row @param {string} day @param {string} zone
 * @param {WorkoutRow[]} [others] @returns {string}
 */
function card(format, row, day, zone, others = []) {
  const { escape, fullDate } = format;
  const endDate = dateKey(row.end_time, zone) === day ? '' : `${escape(fullDate(row.end_time))} · `;
  return `<article><div class="workout-entry-heading"><h4>${escape(titleOf(row))}</h4>
      <span class="workout-badges">${badges(format, row, others)}</span></div>
      <p class="workout-time">${time(format, row.start_time)} – ${endDate}${time(format, row.end_time)}</p>
      ${values(format, row)}${alternatives(format, row, others, day, zone)}</article>`;
}

/**
 * Overlapping sessions from lower-ranked sources stay visible, folded under the primary entry.
 * @param {WorkoutFormat} format @param {WorkoutRow} row @param {WorkoutRow[]} others @param {string} day @param {string} zone
 */
function alternatives(format, row, others, day, zone) {
  if (!others.length) return '';
  const { escape, num } = format;
  const items = others.map(other => `<li class="workout-alternative" ${keyAttributes(format, other)}>
      ${card(format, other, day, zone)}</li>`).join('');
  return `<details class="workout-alternatives"><summary>${escape(text.otherSources(num(others.length)))}</summary>
      <ol aria-label="${escape(text.otherSourcesLabel(titleOf(row)))}">${items}</ol></details>`;
}

/**
 * Keep every source/id pair, even when sessions overlap or share timestamps.
 * @param {WorkoutFormat} format @param {WorkoutRow} row @param {string} day @param {string} zone @param {WorkoutRow[]} others
 */
const entry = (format, row, day, zone, others) =>
  `<li class="workout-entry" ${keyAttributes(format, row)}>${card(format, row, day, zone, others)}</li>`;

/** @param {WorkoutFormat} format @param {Map<string, WorkoutRow[]>} days @param {Map<string, WorkoutRow[]>} nested @param {string} zone */
function daysHtml(format, days, nested, zone) {
  const { escape, fullDate } = format;
  return [...days].map(([day, entries]) => {
    const heading = fullDate(entries[0].start_time);
    const items = entries.map(row => entry(format, row, day, zone, nested.get(String(row.id)) ?? [])).join('');
    return `<li class="workout-day" data-date="${day}" data-key="${day}"><h3><time datetime="${day}">${escape(heading)}</time></h3>
        <ol aria-label="${escape(text.dayLabel(heading))}">${items}</ol></li>`;
  }).join('');
}

/**
 * A secondary row nests under the primary it overlaps; without that primary it stays a top-level entry.
 * @param {WorkoutRow[]} sorted
 */
function nestSecondaries(sorted) {
  const primaryIds = new Set(sorted.filter(row => !secondary(row)).map(row => String(row.id)));
  /** @type {Map<string, WorkoutRow[]>} */
  const nested = new Map();
  /** @type {WorkoutRow[]} */
  const top = [];
  for (const row of sorted) {
    const group = secondary(row) && row.group != null && primaryIds.has(String(row.group)) ? String(row.group) : null;
    if (group === null) { top.push(row); continue; }
    nested.set(group, [...(nested.get(group) ?? []), row]);
  }
  return { nested, top };
}

/** @param {WorkoutRow[]} rows @param {string} zone */
function byDay(rows, zone) {
  /** @type {Map<string, WorkoutRow[]>} */
  const days = new Map();
  for (const row of rows) {
    const day = dateKey(row.start_time, zone);
    days.set(day, [...(days.get(day) ?? []), row]);
  }
  return days;
}

/** @param {WorkoutFormat} format */
export function createWorkoutMarkup(format) {
  /** The month's sessions as markup: newest first, stable source/id tie-breakers, never inferred continuity. @param {WorkoutRow[]} rows @param {string} zone */
  function days(rows, zone) {
    const sorted = [...rows].sort((a, b) =>
      Date.parse(b.start_time) - Date.parse(a.start_time) || `${a.source}:${a.id}`.localeCompare(`${b.source}:${b.id}`));
    const { nested, top } = nestSecondaries(sorted);
    return { html: daysHtml(format, byDay(top, zone), nested, zone), shown: top.length, folded: rows.length - top.length };
  }
  return { days };
}
