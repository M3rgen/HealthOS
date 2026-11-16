// Markup of the workout detail view, built from nodes: every value enters through textContent.
import { num } from './format.js';
import { text } from './i18n/workout-detail.js';
import { valueText } from './workout-charts.js';
import { clock, heartRate, kmh, lapCount, pace } from './workout-track.js';

/** @template {keyof HTMLElementTagNameMap} K @param {K} tag @param {string} [content] @param {string} [className] */
export function el(tag, content = '', className = '') {
  const node = document.createElement(tag);
  if (content) node.textContent = content;
  if (className) node.className = className;
  return node;
}

/** @param {string} label @param {string} className @param {() => void} action */
export function button(label, className, action) {
  const node = el('button', label, `button outline ${className}`);
  node.type = 'button';
  node.addEventListener('click', action);
  return node;
}

/** Speed as km/h or as a pace. @param {number | null | undefined} speed @param {boolean} paced */
const speedText = (speed, paced) => (paced ? valueText('pace', pace(speed)) : valueText('speed', kmh(speed)));
/** @param {number | null | undefined} metres */
const kilometres = metres => (metres == null ? '—' : `${num(metres / 1000, 2)} ${text.units.km}`);
/** @param {number | null | undefined} metres */
const metresText = metres => (metres == null ? '—' : `${num(metres)} m`);

/**
 * The summary cards: times, distance, speed or pace, climb and heart rate.
 * @param {import('./workout-track.js').Track} track @param {boolean} paced
 */
export function summary(track, paced) {
  const { summary: values } = track, heart = heartRate(track);
  const rows = [
    [text.elapsed, clock(values.elapsed_seconds)], [text.moving, clock(values.moving_seconds)],
    [text.distance, kilometres(values.distance)],
    [paced ? text.averagePace : text.averageSpeed, speedText(values.average_speed, paced)],
    [paced ? text.bestPace : text.maximumSpeed, speedText(values.maximum_speed, paced)],
    [text.ascent, metresText(values.ascent)], [text.descent, metresText(values.descent)],
    [text.averageHeart, valueText('heart_rate', heart.average)], [text.maximumHeart, valueText('heart_rate', heart.maximum)],
  ];
  const list = el('dl', '', 'workout-detail-summary');
  list.setAttribute('aria-label', text.summary);
  for (const [term, value] of rows) {
    const item = el('div');
    item.append(el('dt', term), el('dd', value));
    list.append(item);
  }
  return list;
}

/**
 * A group of toggle buttons; the pressed one is the current choice.
 * @template {string} T
 * @param {string} label @param {[T, string][]} options @param {T} chosen @param {(value: T) => void} change
 */
export function toggles(label, options, chosen, change) {
  const group = el('div', '', 'workout-detail-toggle');
  group.setAttribute('role', 'group');
  group.setAttribute('aria-label', label);
  group.append(el('span', label, 'workout-detail-toggle-label'));
  for (const [value, name] of options) {
    const node = button(name, '', () => {
      group.querySelectorAll('button').forEach(other => other.setAttribute('aria-pressed', String(other === node)));
      change(value);
    });
    node.className = 'workout-detail-choice';
    node.setAttribute('aria-pressed', String(value === chosen));
    group.append(node);
  }
  return group;
}

/** @param {import('./workout-track.js').TrackLaps} laps @param {number} i @param {boolean} paced */
function lapCells(laps, i, paced) {
  const at = (/** @type {keyof import('./workout-track.js').TrackLaps} */ key) => laps[key]?.[i] ?? null;
  return [
    clock(at('elapsed_seconds')), kilometres(at('distance')), speedText(at('average_speed'), paced),
    valueText('heart_rate', at('average_heart_rate')), valueText('heart_rate', at('maximum_heart_rate')),
  ];
}

/** The lap table, or nothing for a track without laps. @param {import('./workout-track.js').Track} track @param {boolean} paced */
export function lapTable(track, paced) {
  const count = lapCount(track.laps);
  if (!count) return null;
  const section = el('section', '', 'workout-laps');
  const heading = el('h3', text.laps);
  heading.id = 'workout-laps-title';
  section.setAttribute('aria-labelledby', heading.id);
  const head = [text.lap, text.lapTime, text.lapDistance, paced ? text.pace : text.speed, text.lapAverageHeart, text.lapMaximumHeart];
  const table = el('table'), row = el('tr');
  for (const name of head) {
    const cell = el('th', name);
    cell.scope = 'col';
    row.append(cell);
  }
  table.append(el('thead'), el('tbody'));
  table.tHead?.append(row);
  for (let i = 0; i < count; i++) {
    const line = el('tr'), number = el('th', String(i + 1));
    number.scope = 'row';
    line.append(number, ...lapCells(track.laps, i, paced).map(value => el('td', value)));
    table.tBodies[0].append(line);
  }
  const scroll = el('div', '', 'table-scroll');
  scroll.append(table);
  section.append(heading, scroll);
  return section;
}

/** Heading of the view: what, when and from where, as the list row said. @param {import('./workouts-markup.js').WorkoutRow} row @param {{title: string, when: string}} words */
export function header(row, words) {
  const box = el('div', '', 'workout-detail-heading');
  const title = el('h2', words.title);
  title.id = 'workout-detail-title';
  box.append(title, el('p', words.when, 'workout-detail-when'));
  box.dataset.workoutId = String(row.id);
  return box;
}

/** A small legend for the marks the map and charts share. */
export function legend() {
  const list = el('ul', '', 'workout-detail-legend');
  for (const [kind, label] of [['start', text.start], ['end', text.end], ['lap', text.lapEdge], ['pause', text.pause]]) {
    const item = el('li', label);
    item.dataset.mark = kind;
    list.append(item);
  }
  return list;
}
