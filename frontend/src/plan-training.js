import { button, card, disclosure, el, list, table } from './plan-dom.js';
import { programWeek, weekday } from './plan-model.js';
import { format } from './plan-format.js';
import { text } from './i18n/plan.js';

/** @param {number} day */
const dayName = day => format.civilParts(format.shiftDay('2026-01-04', day), { weekday: 'long' });

/** @param {import('./plan-types').ProgressionRow} row @param {number | null} week */
const isCurrent = (row, week) => week !== null && week >= row.week_start && week <= row.week_end;

/** @param {import('./plan-types').Training} training @param {number | null} week */
export function progressions(training, week) {
  const node = el('div');
  for (const progression of training.progressions ?? []) {
    const grid = table(progression.name, [text.weeks, text.steps, text.duration], progression.rows.map(row =>
      [`${row.week_start}–${row.week_end}`, row.content, row.duration]));
    grid.querySelectorAll('tbody tr').forEach((tr, index) => {
      if (isCurrent(progression.rows[index], week)) {
        tr.classList.add('plan-current');
        tr.setAttribute('aria-current', 'step');
        tr.firstElementChild?.append(el('span', ` · ${text.current}`));
      }
    });
    node.append(grid);
  }
  return node;
}

/** @param {import('./plan-types').Session} session @param {import('./plan-types').Training} training @param {number | null} week */
export function sessionRow(session, training, week) {
  return disclosure(session.name, () => {
    const detail = el('div');
    detail.append(table(session.name, [text.movement, text.sets, text.note], session.exercises.map(exercise =>
      [exercise.movement, exercise.sets_reps, exercise.note ?? '']), true));
    detail.append(progressions(training, week));
    return detail;
  });
}

/** @param {HTMLElement} node @param {import('./plan-types').Plan} plan @param {import('./plan-types').PlanActions} actions */
export function todayTraining(node, plan, actions) {
  const training = plan.training ?? {};
  const schedule = training.weekly_schedule?.find(row => row.weekday === weekday(actions.date));
  const panel = card(text.training);
  if (schedule) {
    panel.append(el('p', schedule.content));
    for (const id of schedule.session_ids ?? []) {
      const session = training.sessions?.find(row => row.id === id);
      if (session) panel.append(sessionRow(session, training, programWeek(actions.state.start_date, actions.date)));
    }
  } else panel.append(el('p', text.noTraining));
  node.append(panel);
}

/** @param {HTMLElement} node @param {import('./plan-types').Plan} plan @param {import('./plan-types').PlanActions} actions */
export function renderTraining(node, plan, actions) {
  const training = plan.training ?? {};
  const week = programWeek(actions.state.start_date, actions.today);
  if (training.weekly_schedule?.length) {
    const schedule = card(text.schedule);
    for (const row of training.weekly_schedule) {
      schedule.append(disclosure(`${dayName(row.weekday)} · ${row.content}`, () => {
        const detail = el('div');
        for (const id of row.session_ids ?? []) {
          const session = training.sessions?.find(item => item.id === id);
          if (session) detail.append(sessionRow(session, training, week));
        }
        detail.append(progressions(training, week));
        return detail;
      }));
    }
    node.append(schedule);
  }
  for (const session of training.sessions ?? []) node.append(sessionRow(session, training, week));
  node.append(progressions(training, week));
  list(node, training.rules, text.rules);
}

/** @param {HTMLElement} node @param {import('./plan-types').Plan} plan @param {import('./plan-types').PlanActions} actions */
export function renderMeasurements(node, plan, actions) {
  list(node, plan.measurements);
  node.append(button(text.addMeasurement, actions.measure));
}
