// The history panel's handle: a drag opens the sheet in proportion to its distance and it stays
// where released; arrow keys move it in steps; a click still opens and closes it whole.
import { queries } from './breakpoints.js';
import { $, $$ } from './dom.js';
import { percent } from './format.js';
import { SIDE_MARGIN, historySheetBounds } from './history-sheet.js';
import { t } from './i18n/index.js';

const monthly = t.history.monthly;
const DRAG_THRESHOLD = 4;
const KEY_STEP = .15;
// Same query as the side-by-side layout in styles/overview.css.
const BESIDE_CARDS = queries.besideCards;

function upperLimit() {
  const warning = $('login-disabled-warning');
  const compact = matchMedia(queries.shortScreen).matches;
  return Math.max(compact ? 0 : innerWidth <= 700 ? 84 : 90, $$('.topbar')[0].getBoundingClientRect().bottom + 8,
    warning && !warning.hidden ? warning.getBoundingClientRect().bottom + 8 : 0);
}

/** @typedef {{openness: number, fromClick?: boolean, closedLeft: number, closedTop: number} & Partial<ReturnType<typeof historySheetBounds>>} Sheet */

/** @typedef {{id: number, x: number, y: number, top: number, left: number}} DragPointer */

/**
 * How far the pointer has moved in the direction that opens the sheet.
 * @param {DragPointer} pointer @param {PointerEvent} event @param {boolean} sideways @param {boolean} expanded
 */
function pointerDistance(pointer, event, sideways, expanded) {
  const fromX = pointer.x - event.clientX, fromY = pointer.y - event.clientY;
  if (!sideways) return fromY;
  return expanded ? fromX : Math.max(fromX, fromY);
}

/**
 * The sheet's openness once the pointer has moved `distance` from where the drag began.
 * @param {DragPointer} pointer @param {DOMRect} closed @param {number} distance @param {boolean} sideways
 */
function dragOpenness(pointer, closed, distance, sideways) {
  const start = sideways ? closed.left - pointer.left : closed.top - pointer.top;
  const range = Math.max(1, sideways ? closed.left - SIDE_MARGIN : closed.top - upperLimit());
  return Math.max(0, Math.min(1, (start + distance) / range));
}

/** Where a drag began: the pointer and the panel's box at that moment. @param {PointerEvent} event @param {HTMLElement} panel @returns {DragPointer} */
function dragStart(event, panel) {
  const box = panel.getBoundingClientRect();
  return { id: event.pointerId, x: event.clientX, y: event.clientY, top: box.top, left: box.left };
}

/**
 * Moves an expanded sheet to `sheet`'s openness and records the bounds it was given.
 * @param {HTMLElement} panel @param {HTMLElement} placeholder @param {Sheet} sheet
 */
function layoutSheet(panel, placeholder, sheet) {
  const sideways = matchMedia(BESIDE_CARDS).matches;
  const bounds = historySheetBounds(placeholder.getBoundingClientRect(), { width: innerWidth, height: innerHeight }, upperLimit(), sheet.openness, sideways);
  Object.assign(sheet, bounds);
  panel.style.top = `${bounds.top}px`;
  // Click-opened sheets keep their responsive CSS insets while being dragged.
  if (!sheet.fromClick || sideways) Object.assign(panel.style, { bottom: `${bounds.bottom}px`, left: `${bounds.left}px`, right: `${bounds.right}px` });
}

/**
 * @param {HTMLElement} toggle
 * @param {object} on
 * @param {(event: PointerEvent) => void} on.start
 * @param {(event: PointerEvent) => void} on.move
 * @param {() => void} on.finish
 * @param {(event: KeyboardEvent) => void} on.resizeByKey
 * @param {(event: MouseEvent) => void} on.click
 */
function wireHandle(toggle, { start, move, finish, resizeByKey, click }) {
  toggle.setAttribute('aria-keyshortcuts', 'ArrowUp ArrowDown');
  toggle.addEventListener('pointerdown', start);
  toggle.addEventListener('pointermove', move);
  toggle.addEventListener('pointerup', finish);
  toggle.addEventListener('pointercancel', finish);
  toggle.addEventListener('keydown', resizeByKey);
  toggle.addEventListener('click', click);
}

/**
 * @param {object} parts
 * @param {HTMLElement} parts.panel
 * @param {HTMLElement} parts.placeholder
 * @param {HTMLElement} parts.toggle
 * @param {HTMLElement} parts.live announces keyboard resizing
 * @param {() => boolean} parts.isExpanded
 * @param {(value: boolean, animate?: boolean) => void} parts.setExpanded
 * @param {() => void} parts.stopAnimation
 */
export function createHistoryDrag({ panel, placeholder, toggle, live, isExpanded, setExpanded, stopAnimation }) {
  /** @type {DragPointer | null} */
  let pointer = null;
  let dragged = false;
  /** @type {Sheet | null} {openness, fromClick?, top?, closedTop?...} */
  let sheet = null;

  function layout() {
    if (!isExpanded()) return;
    if (!sheet) { panel.style.top = `${upperLimit()}px`; return; }
    layoutSheet(panel, placeholder, sheet);
  }

  // The drag and the keyboard both go through here: a closed panel opens on first growth, and a
  // panel opened by clicking becomes resizable by its handle.
  function beginResize() {
    if (!isExpanded()) {
      sheet = /** @type {Sheet} */ ({ openness: 0 });
      setExpanded(true, false);
    }
    sheet ??= /** @type {Sheet} */ ({ openness: 1, fromClick: true });
  }

  /** @param {PointerEvent} event */
  function start(event) {
    if (!event.isPrimary || event.button !== 0) return;
    stopAnimation();
    pointer = dragStart(event, panel);
    dragged = false;
    toggle.setPointerCapture(event.pointerId);
  }
  /** @param {PointerEvent} event */
  function move(event) {
    if (!pointer || pointer.id !== event.pointerId) return;
    const sideways = matchMedia(BESIDE_CARDS).matches;
    const distance = pointerDistance(pointer, event, sideways, isExpanded());
    if (!dragged && Math.abs(distance) < DRAG_THRESHOLD) return;
    if (!isExpanded() && distance <= 0) return;
    dragged = true;
    beginResize();
    /** @type {Sheet} */ (sheet).openness = dragOpenness(pointer, placeholder.getBoundingClientRect(), distance, sideways);
    layout();
  }
  function finish() {
    const rect = panel.getBoundingClientRect();
    if (dragged && sheet && (sheet.sideways ? rect.left >= sheet.closedLeft - 2 : rect.top >= sheet.closedTop - 2)) setExpanded(false, false);
    pointer = null;
  }
  /** Arrow keys move the handle in steps; Enter still opens and closes it whole. @param {KeyboardEvent} event */
  function resizeByKey(event) {
    const step = /** @type {Record<string, number>} */ ({ ArrowUp: KEY_STEP, ArrowDown: -KEY_STEP })[event.key];
    if (step === undefined || (!isExpanded() && step < 0)) return;
    event.preventDefault();
    beginResize();
    /** @type {Sheet} */ (sheet).openness = Math.max(0, Math.min(1, /** @type {Sheet} */ (sheet).openness + step));
    if (/** @type {Sheet} */ (sheet).openness === 0) setExpanded(false, false);
    else layout();
    live.textContent = isExpanded() ? monthly.resized(percent(/** @type {Sheet} */ (sheet).openness * 100, 0)) : monthly.collapsed;
  }

  /** @param {MouseEvent} event */
  function click(event) {
    if (dragged && event.detail !== 0) { dragged = false; return; }
    setExpanded(!isExpanded());
  }
  wireHandle(toggle, { start, move, finish, resizeByKey, click });

  return {
    layout,
    forgetPointer() { pointer = null; },
    closeSheet() { sheet = null; },
  };
}
