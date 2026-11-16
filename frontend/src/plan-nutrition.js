import { card, el, expandable, list, number, table } from './plan-dom.js';
import { dayTypes, mealTotals, programWeek } from './plan-model.js';
import { text } from './i18n/plan.js';
import { todayTraining } from './plan-training.js';

/** @param {import('./plan-types').Meal} meal */
function mealRows(meal) {
  const row = el('tr', '', 'plan-meal'), detail = el('tr');
  const cell = el('td');
  cell.colSpan = 4;
  list(cell, meal.ingredients, text.ingredients);
  if (meal.preparation) cell.append(el('h3', text.preparation), el('p', meal.preparation));
  detail.append(cell);
  const label = el('td');
  label.append(expandable(`${meal.label} · ${meal.name}`, detail));
  row.append(label, el('td', meal.content), el('td', meal.kcal == null ? '—' : number(meal.kcal)),
    el('td', meal.protein_g == null ? '—' : `${number(meal.protein_g)} g`));
  return [row, detail];
}
/** @param {HTMLElement} node @param {import('./plan-types').DayType} day @param {import('./plan-types').Plan} plan */
function meals(node, day, plan) {
  const panel = card(day.name);
  const grid = table(day.name, [text.meal, text.content, 'kcal', text.protein], [], true);
  const body = /** @type {HTMLTableSectionElement} */ (grid.querySelector('tbody'));
  for (const meal of day.meals ?? []) body.append(...mealRows(meal));
  panel.append(grid);
  const total = mealTotals(day.meals ?? []);
  panel.append(el('p', `${text.total}: ${number(total.kcal)} kcal · ${text.protein}: ${number(total.protein)} g`, 'plan-total'));
  panel.append(el('p', text.missingValues, 'fine-print'));
  for (const note of plan.notes?.filter(row => row.day_type_id === day.id) ?? []) list(panel, note.items, text.notes);
  node.append(panel);
}
/** @param {string} label @param {string} value @param {(date: string) => void} change @param {string} id */
function dateField(label, value, change, id) {
  const node = el('label', label, 'plan-date');
  const input = el('input');
  input.type = 'date';
  input.id = id;
  input.value = value;
  input.onchange = () => { if (input.validity.valid) change(input.value); };
  node.append(input);
  return node;
}
/** @param {HTMLElement} node @param {import('./plan-types').Plan} plan @param {import('./plan-types').PlanActions} actions */
export function renderToday(node, plan, actions) {
  // Clearing the day field returns to today.
  node.append(dateField(text.otherDay, actions.date, date => actions.selectDate(date), 'plan-day'));
  node.append(dateField(text.start, actions.state.start_date ?? '', date => actions.changeState({ start_date: date || null }), 'plan-start'));
  renderDay(node, plan, actions);
}
/** Everything below the date fields; a redraw of an open Today keeps the fields and their typing state.
 * @param {HTMLElement} node @param {import('./plan-types').Plan} plan @param {import('./plan-types').PlanActions} actions */
export function renderDay(node, plan, actions) {
  const week = programWeek(actions.state.start_date, actions.date);
  node.append(el('p', week === null ? text.noStart : week === 0 ? text.notStarted : `${text.week}: ${week}`, 'plan-week'));
  const days = dayTypes(plan.day_types ?? [], actions.date);
  for (const day of days) meals(node, day, plan);
  if (!days.length) node.append(el('p', text.noDay));
  if (plan.training) todayTraining(node, plan, actions);
}
/** @param {HTMLElement} node @param {import('./plan-types').Plan} plan */
export function renderNutrition(node, plan) {
  for (const target of plan.targets?.cards ?? []) {
    const panel = card(target.label);
    panel.append(el('p', target.value, 'plan-total'), el('p', target.description));
    node.append(panel);
  }
  list(node, plan.targets?.adjustment_rules, text.adjustment);
  for (const day of plan.day_types ?? []) meals(node, day, plan);
}
/** @param {HTMLElement} node @param {import('./plan-types').Plan} plan */
export function renderBatch(node, plan) {
  for (const recipe of plan.batch_cooking?.recipes ?? []) {
    const panel = card(recipe.name);
    panel.append(el('p', recipe.summary));
    list(panel, recipe.ingredients, text.ingredients);
    panel.append(el('h3', text.preparation), el('p', recipe.preparation), el('p', recipe.portions));
    node.append(panel);
  }
  list(node, plan.batch_cooking?.storage_rules, text.storage);
  list(node, plan.batch_cooking?.substitution_rules, text.substitutions);
}
/** @param {HTMLElement} node @param {import('./plan-types').Plan} plan */
export function renderAssumptions(node, plan) {
  for (const assumption of plan.assumptions ?? []) {
    const panel = card(assumption.title);
    panel.append(el('p', assumption.text));
    node.append(panel);
  }
  if (plan.meta?.warning) node.append(el('p', plan.meta.warning, 'plan-warning'));
}
