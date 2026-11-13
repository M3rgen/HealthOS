import { errorText } from './api.js';
import { enhanceMonthInput } from './calendar.js';
import { civilParts } from './dates.js';
import { $, $$, closest, setText } from './dom.js';
import { createHistoryDrag } from './history-drag.js';
import { t } from './i18n/index.js';

const monthly = t.history.monthly;
/** @param {string} selected yyyy-mm */
const monthTitle = selected => civilParts(`${selected}-01`, {month:'long',year:'numeric'});

/** @typedef {import('./state.js').DashboardData} DashboardData */
/** @typedef {{request: typeof import('./api.js').request, getZone: () => string, getToday: () => string}} MonthSource */

/** Empties the table while a month loads; the export link waits for it. @param {HTMLElement} toggle */
function showMonthLoading(toggle) {
  $('history-status').hidden = false;
  $('history-status').textContent = monthly.loading;
  $('history-title').textContent = monthly.title;
  $('range-caption').textContent = '';
  $('history-rows').replaceChildren();
  if (document.activeElement === $('export-button')) toggle.focus();
  $('export-button').hidden = true;
}

/** Keeps the panel's grid row while the panel is out of flow. @param {HTMLElement} panel */
function createPlaceholder(panel) {
  const placeholder = document.createElement('div');
  placeholder.hidden = true;
  // Let the grid size this row again when the viewport or warning changes.
  placeholder.style.minHeight = '0';
  panel.before(placeholder);
  return placeholder;
}

/** Announces keyboard resizing. @param {HTMLElement} panel */
function createLiveRegion(panel) {
  const live = document.createElement('p');
  live.className = 'sr-only';
  live.setAttribute('aria-live', 'polite');
  panel.append(live);
  return live;
}

/** Plays the panel's size change from its previous box to its new one. @param {HTMLElement} panel @param {DOMRect} before @param {DOMRect} after */
function morphFrom(panel, before, after) {
  const from = `translate(${before.left - after.left}px,${before.top - after.top}px) scale(${before.width / after.width},${before.height / after.height})`;
  return panel.animate([
    {transformOrigin:'top left',transform:from},
    {transformOrigin:'top left',transform:'none'},
  ], {duration:380,easing:'cubic-bezier(.16,1,.3,1)'});
}

/** One month of the history table, ending today at the latest, as request parameters. @param {string} selected yyyy-mm @param {() => string} getToday */
function monthQuery(selected, getToday) {
  const [year, number] = selected.split('-').map(Number);
  const last = `${selected}-${String(new Date(Date.UTC(year, number, 0)).getUTCDate()).padStart(2,'0')}`;
  const end = last > getToday() ? getToday() : last;
  return {days: String(Number(end.slice(-2))), end_date: end};
}

/**
 * Checks the chosen month, titles the panel with it and fetches it.
 * @param {string} selected yyyy-mm @param {string} latest the newest month allowed @param {MonthSource} source
 * @returns {Promise<DashboardData>}
 */
async function fetchMonth(selected, latest, { request, getZone, getToday }) {
  if (!/^\d{4}-\d{2}$/.test(selected) || selected > latest) throw new Error(monthly.invalidMonth);
  setText($('history-title'), monthTitle(selected));
  return request(`/api/dashboard?${new URLSearchParams({...monthQuery(selected, getToday), timezone:getZone()})}`);
}

/** The panel's open or closed face, once it has its new state. @param {boolean} value @param {{panel: HTMLElement, toggle: HTMLElement, layout: () => void}} parts */
function showExpanded(value, { panel, toggle, layout }) {
  // The sheet covers the summary cards; keep Tab on controls still visible.
  $$('.overview-grid').forEach(grid => { grid.inert = value; });
  panel.classList.toggle('expanded', value);
  if (value) layout();
  $('history-months').hidden = !value;
  toggle.setAttribute('aria-expanded', String(value));
  toggle.setAttribute('aria-label', value ? monthly.back : monthly.open);
}

/**
 * Lays the sheet out again whenever the warning, the placeholder or the window changes size.
 * @param {HTMLElement} placeholder @param {ReturnType<typeof createHistoryDrag>} drag
 */
function watchLayout(placeholder, drag) {
  const observer = new ResizeObserver(drag.layout);
  observer.observe($('login-disabled-warning'));
  observer.observe(placeholder);
  window.addEventListener('resize', () => { drag.forgetPointer(); drag.layout(); });
}

/** Escape closes the sheet only when nothing above it (a dialog, a popover, Umay) takes the key. @param {KeyboardEvent} event @param {HTMLElement} panel */
function escapeIsForPanel(event, panel) {
  return event.key === 'Escape' && !event.defaultPrevented && !panel.closest('[hidden]') &&
    !document.querySelector('dialog:modal, :popover-open') && !closest(event, '#assistant-dialog');
}

/** Hides the status line, handing focus to the toggle if the line held it. @param {HTMLElement} toggle */
function hideStatus(toggle) {
  if (document.activeElement === $('history-status')) toggle.focus();
  $('history-status').hidden = true;
}

/** Puts the panel back in its grid row. @param {{panel: HTMLElement, placeholder: HTMLElement, drag: ReturnType<typeof createHistoryDrag>}} parts */
function collapseSheet({ panel, placeholder, drag }) {
  placeholder.hidden = true;
  $('export-button').hidden = false;
  for (const property of ['top', 'bottom', 'left', 'right']) panel.style.removeProperty(property);
  drag.closeSheet();
}

/** @param {MonthSource & {paint: typeof import('./history-table.js').paintHistory}} options */
export function setupHistoryPanel({request, getZone, getToday, paint}) {
  const panel = $('history-panel'), toggle = $('history-toggle'), month = $('history-month');
  const placeholder = createPlaceholder(panel);
  const live = createLiveRegion(panel);
  let expanded = false, version = 0;
  /** @type {DashboardData | undefined} */
  let weekly;
  /** @type {Animation | undefined} */
  let animation;
  month.value = month.max = getToday().slice(0, 7);
  enhanceMonthInput(month);
  const drag = createHistoryDrag({ panel, placeholder, toggle, live, isExpanded: () => expanded, setExpanded, stopAnimation: () => animation?.cancel() });
  watchLayout(placeholder, drag);

  // The month and zone the open sheet shows. The poll asks for the same month every minute and
  // must leave the reader where they are (rows, scroll, focus, no status line); only another month
  // or zone starts again from an empty, loading table.
  /** @type {string | null} */
  let paintedFor = null;

  /** @param {DashboardData} [data] */
  async function update(data = weekly) {
    weekly = data;
    const token = ++version;
    month.max = getToday().slice(0, 7);
    if (!expanded) { paintedFor = null; hideStatus(toggle); if (weekly) paint(weekly, { current: weekly.end_date === getToday() }); return; }
    const selected = month.value, key = `${selected}|${getZone()}`;
    const repeat = paintedFor === key;
    if (!repeat) { paintedFor = null; showMonthLoading(toggle); }
    try {
      const result = await fetchMonth(selected, month.max, { request, getZone, getToday });
      if (token !== version) return;
      paint(result);
      setText($('history-title'), monthTitle(selected));
      paintedFor = key;
      hideStatus(toggle);
      $('export-button').hidden = false;
    } catch (error) {
      // A failed repeat keeps the month on screen; the dashboard's notice reports the outage.
      if (token === version && !repeat) $('history-status').textContent = monthly.retry(errorText(error));
    }
  }

  function showPlaceholder() {
    placeholder.hidden = false;
    month.value = getToday().slice(0,7);
  }

  /** @param {boolean} value @param {boolean} [animate] */
  function setExpanded(value, animate = true) {
    if (expanded === value) return;
    if (!value && $('history-months').contains(document.activeElement)) toggle.focus();
    animation?.cancel();
    const before = panel.getBoundingClientRect();
    if (value) showPlaceholder();
    expanded = value;
    showExpanded(value, { panel, toggle, layout: drag.layout });
    if (!value) collapseSheet({ panel, placeholder, drag });
    update();
    const after = panel.getBoundingClientRect();
    if (animate && !matchMedia('(prefers-reduced-motion: reduce)').matches) animation = morphFrom(panel, before, after);
  }
  document.addEventListener('keydown', event => {
    if (!expanded || !escapeIsForPanel(event, panel)) return;
    event.preventDefault();
    setExpanded(false); toggle.focus();
  });
  month.addEventListener('change', () => update());
  $('history-current').addEventListener('click', () => { month.value = getToday().slice(0,7); update(); });
  return {update};
}
