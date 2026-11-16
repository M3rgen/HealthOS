import { format } from './plan-format.js';

// Civil-day arithmetic ignores device offsets and DST. Overlapping day types keep document order.
/** @param {string} date */
export const weekday = date => format.civilDate(date).getUTCDay() || 7;

/** @param {import('./plan-types').DayType[]} days @param {string} date */
export const dayTypes = (days, date) => days.filter(day => day.weekdays?.includes(weekday(date)));

/** Day number of the Monday that opens the civil week of `date`. @param {string} date */
const monday = date => Math.round(format.civilDate(date).getTime() / 86400000) - weekday(date) + 1;

/** Weeks run Monday to Sunday; week one is the week that holds the saved start date, so a
 * Wednesday start counts from that week's Monday. The saved date itself stays as chosen.
 * Null means unset, zero means the program has not started.
 * @param {string | null} start @param {string} date */
export function programWeek(start, date) {
  if (!start) return null;
  const days = monday(date) - monday(start);
  return Number.isFinite(days) ? Math.max(0, days / 7 + 1) : null;
}

/** Missing nutrition values are excluded, never inferred from ingredients.
 * @param {import('./plan-types').Meal[]} meals */
export function mealTotals(meals) {
  return {
    kcal: meals.reduce((sum, meal) => sum + (meal.kcal ?? 0), 0),
    protein: meals.reduce((sum, meal) => sum + (meal.protein_g ?? 0), 0),
  };
}

/** Keep currencies separate; an item without a price contributes nothing.
 * @param {import('./plan-types').ShoppingItem[]} items */
export function priceTotals(items) {
  /** @type {Record<string, number>} */
  const totals = {};
  for (const item of items) {
    if (item.estimated_price != null && item.currency)
      totals[item.currency] = (totals[item.currency] ?? 0) + item.estimated_price;
  }
  return totals;
}

/** Empty objects and rows do not produce navigation buttons. @param {unknown} value */
function hasContent(value) {
  if (Array.isArray(value)) return value.length > 0;
  if (value && typeof value === 'object') return Object.values(value).some(hasContent);
  return Boolean(value);
}
/** @param {import('./plan-types').Plan} plan */
const dayMeals = plan => (plan.day_types ?? []).flatMap(day => day.meals ?? []);
/** @param {import('./plan-types').Plan} plan */
export function sections(plan) {
  const nutrition = hasContent([...dayMeals(plan),
    ...(plan.notes ?? []).flatMap(note => note.items)]) || hasContent(plan.targets);
  const training = hasContent(plan.training);
  const shopping = hasContent((plan.shopping?.groups ?? []).flatMap(group => group.items)) || hasContent(plan.shopping?.one_off);
  return Object.entries({
    today: hasContent(dayMeals(plan)) || training,
    nutrition, batch: hasContent(plan.batch_cooking), shopping, training,
    measurements: hasContent(plan.measurements), assumptions: hasContent(plan.assumptions) || hasContent(plan.meta?.warning),
  }).filter(([, exists]) => exists).map(([key]) => key);
}
