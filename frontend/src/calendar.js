import { civilDate as civil, civilParts } from './dates.js';
import { $, closest, setText } from './dom.js';
import { t } from './i18n/index.js';

/** @param {Date} date */
const key = (date) => date.toISOString().slice(0, 10);

/** @param {HTMLElement} popover @param {HTMLElement} trigger */
function placeBelow(popover, trigger) {
  const rect = trigger.getBoundingClientRect();
  popover.style.left = `${Math.max(12, Math.min(rect.right - popover.offsetWidth, window.innerWidth - popover.offsetWidth - 12))}px`;
  popover.style.top = `${rect.bottom + 12}px`;
}

/** @param {Date} date @param {{month: Date, value: string, today: string, focused: string}} view */
function dayButton(date, { month, value, today, focused }) {
  const valueKey = key(date);
  const button = document.createElement('button');
  button.type = 'button';
  button.textContent = String(date.getUTCDate());
  button.dataset.date = valueKey;
  button.className = 'calendar-day';
  button.classList.toggle('outside-month', date.getUTCMonth() !== month.getUTCMonth());
  button.classList.toggle('selected', valueKey === value);
  button.classList.toggle('is-today', valueKey === today);
  button.setAttribute('aria-label', civilParts(valueKey, { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' }));
  button.setAttribute('aria-pressed', String(valueKey === value));
  if (valueKey === today) button.setAttribute('aria-current', 'date');
  button.disabled = valueKey > today;
  button.tabIndex = valueKey === focused ? 0 : -1;
  return button;
}

/** The selected day in the header button and the popover. @param {string} value @param {HTMLElement} trigger */
function paintSelection(value, trigger) {
  const label = civilParts(value, { day: 'numeric', month: 'long', year: 'numeric' });
  ($('header-date-text') ?? trigger).textContent = label;
  // The short form replaces it on the short top bar (topbar.css); only one of the two is ever shown.
  $('header-date-short').textContent = civilParts(value, { day: 'numeric', month: 'short' });
  trigger.setAttribute('aria-label', t.calendar.trigger(label));
  $('calendar-selected').textContent = civilParts(value, { weekday: 'long', day: 'numeric', month: 'long' });
}

/** Six weeks from the Monday on or before the 1st. @param {HTMLElement} days @param {{month: Date, value: string, getToday: () => string, focused: string}} view */
function fillDays(days, { month, value, getToday, focused }) {
  const start = new Date(month);
  start.setUTCDate(1 - (month.getUTCDay() + 6) % 7);
  days.replaceChildren();
  for (let index = 0; index < 42; index++) {
    const date = new Date(start);
    date.setUTCDate(start.getUTCDate() + index);
    days.append(dayButton(date, { month, value, today: getToday(), focused }));
  }
}

/** The move a key makes from `date`, in days; undefined for any other key. @param {string} pressed @param {Date} date */
function dayOffsetForKey(pressed, date) {
  const weekday = (date.getUTCDay() + 6) % 7;
  /** @type {Record<string, number>} */
  const offsets = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7, Home: -weekday, End: 6 - weekday };
  return pressed in offsets ? offsets[pressed] : undefined;
}

/** @param {HTMLElement} popover @param {HTMLElement} trigger @param {() => void} onOpen */
function watchPopover(popover, trigger, onOpen) {
  popover.addEventListener('beforetoggle', event => {
    if (event.newState === 'closed' && popover.contains(document.activeElement)) trigger.focus();
  });
  popover.addEventListener('toggle', (event) => {
    const open = event.newState === 'open';
    trigger.setAttribute('aria-expanded', String(open));
    if (open) onOpen();
  });
}

/** Whether the month after `month` lies entirely in the future. @param {Date} month @param {string} today */
function nextMonthIsFuture(month, today) {
  return key(new Date(Date.UTC(month.getUTCFullYear(), month.getUTCMonth() + 1, 1, 12))) > today;
}

/** @param {(offset: number) => void} onStep */
function wireMonthSteps(onStep) {
  for (const [id, offset] of /** @type {[string, number][]} */ ([['calendar-prev', -1], ['calendar-next', 1]])) {
    $(id).addEventListener('click', () => onStep(offset));
  }
}

/** @param {{getToday: () => string, getSelected: () => string | null, onSelect: (value: string) => void}} options */
export function setupCalendar({ getToday, getSelected, onSelect }) {
  const trigger = $('header-date');
  const popover = $('date-calendar');
  const days = $('calendar-days');
  /** @type {Date} */
  let month;
  /** @type {string} */
  let focused;
  const selected = () => getSelected() || getToday();
  const setMonth = (/** @type {Date} */ date) => { month = new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), 1, 12)); };

  function render() {
    const restoreFocus = days.contains(document.activeElement);
    const value = selected();
    paintSelection(value, trigger);
    setText($('calendar-month'), civilParts(key(month), { month: 'long', year: 'numeric' }));
    fillDays(days, { month, value, getToday, focused });
    const nextFocused = document.activeElement === $('calendar-next');
    $('calendar-next').disabled = nextMonthIsFuture(month, getToday());
    if (restoreFocus || (nextFocused && $('calendar-next').disabled)) focusDay();
  }

  const position = () => placeBelow(popover, trigger);
  function focusDay() { /** @type {HTMLElement | null} */ (days.querySelector(`[data-date="${focused}"]`))?.focus(); }
  /** @param {string} value */
  function choose(value) {
    onSelect(value);
    focused = value;
    setMonth(civil(value));
    render();
    popover.hidePopover();
    trigger.focus();
  }
  watchPopover(popover, trigger, () => { focused = selected(); setMonth(civil(focused)); render(); position(); focusDay(); });
  window.addEventListener('resize', () => { if (popover.matches(':popover-open')) position(); });
  wireMonthSteps(offset => {
    month.setUTCMonth(month.getUTCMonth() + offset);
    focused = key(month);
    render();
  });
  days.addEventListener('click', (event) => {
    const button = /** @type {HTMLButtonElement | null} */ (closest(event, '[data-date]'));
    if (button && !button.disabled) choose(/** @type {string} */ (button.dataset.date));
  });
  days.addEventListener('keydown', (event) => {
    const button = closest(event, '[data-date]');
    if (!button) return;
    const date = civil(/** @type {string} */ (button.dataset.date));
    const offset = dayOffsetForKey(event.key, date);
    if (offset === undefined) return;
    event.preventDefault();
    date.setUTCDate(date.getUTCDate() + offset);
    if (key(date) > getToday()) return;
    focused = key(date);
    setMonth(date);
    render();
    focusDay();
  });
  $('calendar-today').addEventListener('click', () => choose(getToday()));
  focused = selected();
  setMonth(civil(focused));
  render();
  return { update: render };
}

// Native month inputs render their text in the browser's UI locale. Where the
// browser supports type=month, overlay the value as a label in the interface
// language ("Ekim 2026") while the native control stays focusable, keyboard-editable and
// opens its own picker. Browsers without a month picker keep the plain field.
/** @param {HTMLInputElement | null | undefined} input */
export function enhanceMonthInput(input) {
  if (!input || input.type !== 'month' || input.closest('.month-field')) return;
  const field = document.createElement('span');
  field.className = 'month-field';
  const label = document.createElement('span');
  label.className = 'month-label';
  label.setAttribute('aria-hidden', 'true');
  input.before(field);
  field.append(input, label);
  const render = () => {
    const value = input.value;
    /** @param {'long' | 'short'} month */
    const text = (month) => /^\d{4}-\d{2}$/.test(value) ? civilParts(`${value}-01`, { month, year: 'numeric' }) : '—';
    label.textContent = text('long');
    // Large text in a narrow row: "Eki 2026" rather than a month name cut off at "Ekim 20…".
    if (label.scrollWidth > label.clientWidth) label.textContent = text('short');
  };
  const native = /** @type {PropertyDescriptor & {get: () => string, set: (value: string) => void}} */ (Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value'));
  Object.defineProperty(input, 'value', {
    configurable: true,
    get() { return native.get.call(this); },
    set(value) { native.set.call(this, value); render(); },
  });
  input.addEventListener('input', render);
  input.addEventListener('change', render);
  input.addEventListener('click', () => { try { input.showPicker(); } catch { /* Focus still allows keyboard entry. */ } });
  // The label's width follows the row (viewport, browser text size, a page shown from hidden).
  if (typeof ResizeObserver === 'function') new ResizeObserver(render).observe(label);
  render();
}
