import { errorText } from './api.js';
import { enhanceMonthInput } from './calendar.js';
import { civilDate, dateKey, zonedInstant } from './dates.js';
import { $, closest, setAttr, setText } from './dom.js';
import { morphChildren } from './dom-morph.js';
import { t } from './i18n/index.js';
import { sourceLabel } from './data-types.js';
import { createWorkoutMarkup, titleOf } from './workouts-markup.js';

const text = t.workouts;

/**
 * The previous/next month buttons: moves `month` by one and asks for the new month.
 * @param {HTMLInputElement} month @param {() => boolean} validMonth @param {() => unknown} update
 */
function wireMonthSteps(month, validMonth, update) {
  for (const [id, offset] of /** @type {[('workout-prev'|'workout-next'), number][]} */ ([['workout-prev',-1],['workout-next',1]])) {
    $(id).addEventListener('click', () => {
      if (!validMonth()) return;
      const date = civilDate(`${month.value}-01`);
      date.setUTCMonth(date.getUTCMonth()+offset);
      month.value = date.toISOString().slice(0,7);
      update();
    });
  }
}

/** @typedef {Record<'exercise_type'|'start_time'|'end_time'|'calories_kcal'|'distance_km', HTMLInputElement>} WorkoutFields */

/** @param {WorkoutFields} fields @param {Date} start @param {Date} end */
function workoutPayload(fields, start, end) {
  return {
    exercise_type:fields.exercise_type.value.trim(),start_time:start.toISOString(),end_time:end.toISOString(),
    calories_kcal:fields.calories_kcal.value === '' ? null : Number(fields.calories_kcal.value),
    distance_meters:fields.distance_km.value === '' ? null : Number(fields.distance_km.value)*1000,
  };
}

/**
 * The add-a-session form: one save at a time, and a saved session opens the month it belongs to.
 * @param {{request: typeof import('./api.js').request, getZone: () => string, month: HTMLInputElement, update: () => Promise<void>}} parts
 */
function setupWorkoutForm({ request, getZone, month, update }) {
  const form = $('workout-form'), editor = $('workout-editor');
  const fields = /** @type {WorkoutFields} */ (/** @type {unknown} */ (form.elements));
  let saving = false;
  async function save() {
    const start = zonedInstant(fields.start_time.value, getZone()), end = zonedInstant(fields.end_time.value, getZone());
    // `required` accepts spaces alone; the server would refuse them without naming the field.
    if (!fields.exercise_type.value.trim()) throw new Error(text.nameRequired);
    if (end <= start || end > new Date()) throw new Error(text.invalidRange);
    await request('/api/dashboard/workouts', {
      method:'POST', headers:{'Content-Type':'application/json','X-HealthOS-Request':'1'},
      body:JSON.stringify(workoutPayload(fields, start, end)),
    });
    month.value = dateKey(start,getZone()).slice(0,7);
    form.reset(); editor.open = false; $('workout-add').focus(); await update();
  }
  $('workout-add').addEventListener('click', () => {
    editor.open = true;
    fields.exercise_type.focus();
    // The full editor can exceed the panel height; reveal its heading, keeping field focus.
    editor.querySelector('summary')?.scrollIntoView({ block: 'nearest' });
  });
  $('workout-cancel').addEventListener('click', () => { editor.open = false; $('workout-add').focus(); });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (saving) return;
    saving = true;
    $('workout-error').textContent = ''; $('workout-save').setAttribute('aria-disabled', 'true');
    try {
      await save();
    } catch (error) { $('workout-error').textContent = errorText(error); }
    finally { saving = false; $('workout-save').setAttribute('aria-disabled', 'false'); }
  });
}

/** The month input and its previous/next limits. @param {HTMLInputElement} month @param {() => string} getToday */
function setupMonthField(month, getToday) {
  month.value = month.max = getToday().slice(0,7);
  month.min = '0001-01';
  enhanceMonthInput(month);
  const validMonth = () => /^\d{4}-(0[1-9]|1[0-2])$/.test(month.value) && month.value >= month.min && month.value <= month.max;
  function controls() {
    const focused = document.activeElement;
    month.max = getToday().slice(0,7);
    $('workout-prev').disabled = !validMonth() || month.value === month.min;
    $('workout-next').disabled = !validMonth() || month.value === month.max;
    if ((focused === $('workout-prev') || focused === $('workout-next')) && /** @type {HTMLButtonElement} */ (focused).disabled) month.focus();
  }
  return { validMonth, controls };
}

/**
 * A workout's "Route and charts" button opens its detail view (workout-detail.js, loaded then).
 * @param {{rowsElement: HTMLElement, panel: HTMLElement, status: HTMLElement, request: typeof import('./api.js').request,
 *   update: () => Promise<void>, fullDate: (value: string) => string, timeOf: (value: string) => string}} parts
 * @returns {{shown: import('./workouts-markup.js').WorkoutRow[]}} the rows on screen, which paintRows keeps current
 */
function wireTrackDetail({ rowsElement, panel, status, request, update, fullDate, timeOf }) {
  /** @type {{shown: import('./workouts-markup.js').WorkoutRow[]}} */
  const tracks = { shown: [] };
  /** @param {import('./workouts-markup.js').WorkoutRow} row */
  function open(row) {
    const when = `${fullDate(row.start_time)} · ${timeOf(row.start_time)}–${timeOf(row.end_time)} · ${sourceLabel(row.source_code, row.source)}`;
    import('./workout-detail.js')
      .then(module => module.openWorkoutDetail({
        request, row, list: panel, words: { title: titleOf(row), when }, refresh: update, gone: message => setText(status, message),
      }))
      .catch(() => setText(status, t.common.pageOutdated));
  }
  rowsElement.addEventListener('click', event => {
    const id = closest(event, '[data-track-open]')?.closest('[data-workout-id]')?.getAttribute('data-workout-id');
    const row = tracks.shown.find(candidate => String(candidate.id) === id);
    if (row) open(row);
  });
  return tracks;
}

/**
 * @param {{request: typeof import('./api.js').request, getZone: () => string, getToday: () => string}
 *   & import('./workouts-markup.js').WorkoutFormat} options
 */
export function setupWorkouts({request,getZone,getToday,escape,num,duration,fullDate,timeOf}) {
  const month = $('workout-month'), timeline = $('workout-timeline');
  const panel = /** @type {HTMLElement} */ (month.closest('.workouts-panel'));
  const rowsElement = $('workout-rows'), status = $('workout-status'), retry = $('workout-retry');
  const markup = createWorkoutMarkup({escape,num,duration,fullDate,timeOf});
  let version = 0;
  // The month and zone the list on screen shows. A refresh of the same list happens every minute
  // and must leave the reader where they are; only a different list starts again from the top.
  /** @type {string | null} */
  let paintedFor = null;
  const { validMonth, controls } = setupMonthField(month, getToday);

  function hideRetry() {
    if (document.activeElement === retry) month.focus();
    retry.hidden = true;
  }
  function showLoading() {
    paintedFor = null;
    setText(status, text.loading);
    rowsElement.replaceChildren();
    timeline.setAttribute('aria-busy','true');
    hideRetry();
    delete panel.dataset.empty;
  }
  /** Builds the escaped day cards and their matching count. @param {import('./workouts-markup.js').WorkoutRow[]} rows @param {string} zone */
  function workoutDaysMarkup(rows, zone) {
    const { html, shown, folded } = markup.days(rows,zone);
    setText(status, rows.length ? text.count(num(shown), folded ? num(folded) : '') : text.empty);
    return html;
  }
  const tracks = wireTrackDetail({ rowsElement, panel, status, request, update, fullDate, timeOf });
  /** @param {import('./workouts-markup.js').WorkoutRow[]} rows @param {string} zone @param {string} key */
  function paintRows(rows, zone, key) {
    tracks.shown = rows;
    morphChildren(rowsElement, workoutDaysMarkup(rows, zone));
    if (paintedFor !== key) timeline.scrollTop = 0;
    paintedFor = key; hideRetry();
    panel.toggleAttribute('data-empty', !rows.length);
  }
  async function update() {
    const token = ++version;
    controls();
    const selectedMonth = month.value, zone = getZone(), key = `${selectedMonth}|${zone}`;
    const current = () => token === version && month.value === selectedMonth && getZone() === zone;
    if (paintedFor !== key) showLoading();
    setText($('workout-zone'), text.zoneNote(zone)); setText($('workout-timezone'), text.entryZone(zone));
    try {
      if (!validMonth()) throw new Error(text.invalidMonth);
      /** @type {import('./workouts-markup.js').WorkoutRow[]} */
      const rows = await request(`/api/dashboard/workouts?${new URLSearchParams({month:`${selectedMonth}-01`,timezone:zone})}`);
      if (current()) paintRows(rows, zone, key);
    } catch (error) {
      if (current()) {
        setText(status, errorText(error));
        retry.hidden = false;
      }
    } finally {
      if (current()) setAttr(timeline, 'aria-busy', 'false');
    }
  }
  wireMonthSteps(month, validMonth, update);
  setupWorkoutForm({ request, getZone, month, update });
  $('workout-timezone').textContent = text.entryZone(getZone());
  retry.addEventListener('click', update); month.addEventListener('change', update);
  controls();
  return {update};
}

/** @type {{update: () => Promise<void>} | undefined} */
let active;
/** The page's one controller, made on its first visit; every later call refreshes the list. @param {Parameters<typeof setupWorkouts>[0]} options */
export function loadWorkouts(options) {
  active ??= setupWorkouts(options);
  return active.update();
}
