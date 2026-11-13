// The overview's profile figures and three metric cards, drawn from one /api/dashboard response.
import { $ } from './dom.js';
import { civilShort } from './dates.js';
import { duration, num } from './format.js';
import { t } from './i18n/index.js';
import { bars, line } from './sparklines.js';

const { cards } = t.overview;
const { bpm } = t.common;

/** @typedef {{date: string, partial?: boolean, steps?: number|null, sleep_seconds?: number|null, resting_bpm?: number|null}} DayRow */

/** The three metric cards for the newest day; a missing value says so in its note. @param {DayRow} first */
function paintDayValues(first) {
  const shownDate = civilShort(first.date);
  $('steps-label').textContent = first.partial ? cards.stepsToday : cards.stepsThatDay;
  $('sleep-label').textContent = first.partial ? cards.sleepLastNight : cards.sleepThatNight;
  $('steps-value').textContent = num(first.steps);
  $('sleep-value').textContent = duration(first.sleep_seconds);
  $('heart-value').textContent = first.resting_bpm == null ? '—' : bpm(num(first.resting_bpm));
  $('steps-note').textContent = first.steps == null ? cards.noSteps : first.partial ? cards.dayOngoing : shownDate;
  $('steps-note').classList.toggle('ongoing-badge', Boolean(first.partial && first.steps != null));
  $('sleep-note').textContent = first.sleep_seconds == null ? cards.noSleep : shownDate;
  $('heart-note').textContent = first.resting_bpm == null ? cards.noDailySummary : shownDate;
}

/**
 * @param {import('./state.js').DashboardData} data
 * @param {{profileValues: ReturnType<typeof import('./profile-values.js').setupProfileValues>, body: ReturnType<typeof import('./body-loader.js').setupBodyLoader>}} parts
 */
export function paintCards(data, { profileValues, body }) {
  const first = data.days[0];
  profileValues.update(data.profile);
  // Drive the 3D body's morphs from the latest profile measurements (missing values keep the basis).
  body.setBodyMetrics({
    heightCm: data.profile.height?.value,
    weightKg: data.profile.weight?.value,
    bodyFatPct: data.profile.body_fat?.value,
    musclePct: data.profile.muscle?.value,
  });
  paintDayValues(first);
  const stepLabels = data.charts.steps.map((v, i) => `${i}:00 ${num(v)}`),
    sleepDays = [...data.days].reverse(),
    sleepLabels = sleepDays.map(row => `${civilShort(row.date)}: ${duration(row.sleep_seconds)}`);
  $('steps-chart').innerHTML = bars(data.charts.steps, 'var(--step-chart)', undefined, stepLabels);
  $('sleep-chart').innerHTML = bars(sleepDays.map(row => row.sleep_seconds), 'var(--sleep-light)', 'var(--sleep-dark)', sleepLabels);
  $('sleep-axis').textContent = cards.days(num(data.days.length));
  $('heart-chart').innerHTML = line(data.charts.heart_rate);
  $('steps-chart').setAttribute('aria-label', cards.stepsChart(stepLabels.join(', ')));
  $('heart-chart').setAttribute('aria-label', cards.heartChart(data.charts.heart_rate.map((v, i) => `${i}:00 ${num(v, 1)}`).join(', ')));
  $('sleep-chart').setAttribute('aria-label', cards.sleepChart(sleepLabels.join(', ')));
}

/** No response has ever arrived: the cards stop saying they are loading. */
export function paintCardsFailed() {
  for (const id of ['steps-note', 'sleep-note', 'heart-note']) $(id).textContent = cards.notLoaded;
}
