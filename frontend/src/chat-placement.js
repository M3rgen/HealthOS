// Where the Umay bubble and its panel sit: the bubble can be dragged anywhere on the screen, the
// panel opens next to it, and on phones it covers the screen with everything behind it inert.
import { queries } from './breakpoints.js';
import { setupLauncherDrag } from './chat-drag.js';

const EDGE = 8, GAP = 12, LAUNCHER_EDGE = 12;

// At the shared 900 px boundary (or on short screens), everything behind the panel goes inert
// (hidden from Tab and screen readers) until it closes. Wider screens keep it a side panel.
const MODAL_VIEW = queries.umayModal;

/** @typedef {{left: number, top: number, width: number, height: number}} Box */

/** @returns {Box} the visible area, which the on-screen keyboard can shrink */
function viewportBox() {
  const viewport = window.visualViewport;
  return {
    left: viewport?.offsetLeft ?? 0, top: viewport?.offsetTop ?? 0,
    width: viewport?.width ?? document.documentElement.clientWidth,
    height: viewport?.height ?? window.innerHeight,
  };
}

/** @param {Box} view @returns {Box} */
function modalPanel(view) {
  const width = Math.min(560, view.width - EDGE * 2), height = view.height - EDGE * 2;
  return { width, height, left: view.left + (view.width - width) / 2, top: view.top + EDGE };
}

/** @param {Box} view @param {{top: number, bottom: number, right: number}} bubble @returns {Box} */
function anchoredPanel(view, bubble) {
  const above = bubble.top - view.top - EDGE - GAP;
  const below = view.top + view.height - EDGE - bubble.bottom - GAP;
  const useAbove = above >= 300 || above >= below;
  const available = Math.max(0, useAbove ? above : below);
  const fillViewport = available < Math.min(220, view.height - EDGE * 2);
  const height = Math.min(view.width <= 540 ? 700 : 740, fillViewport ? view.height - EDGE * 2 : available);
  const width = Math.min(560, view.width - EDGE * 2);
  const left = Math.max(view.left + EDGE, Math.min(bubble.right - width, view.left + view.width - EDGE - width));
  const anchored = useAbove ? bubble.top - GAP - height : bubble.bottom + GAP;
  return { width, height, left, top: fillViewport ? view.top + EDGE : anchored };
}

const FOCUSABLE = 'a[href], button, input, select, textarea, summary, [tabindex]:not([tabindex="-1"])';

/**
 * Page controls whose middle lies under the open side panel; focus there could not be seen.
 * Native dialogs and the calendar stack above the panel, and a focusable container's own
 * controls are judged one by one.
 * @param {HTMLElement} dialog @param {HTMLElement} launcher @returns {HTMLElement[]}
 */
function controlsUnderPanel(dialog, launcher) {
  const panel = dialog.getBoundingClientRect();
  return /** @type {HTMLElement[]} */ ([...document.querySelectorAll(FOCUSABLE)]).filter(element => {
    if (dialog.contains(element) || element === launcher || element === document.activeElement) return false;
    if (element.closest('dialog, [popover]') || element.querySelector(FOCUSABLE)) return false;
    const box = element.getBoundingClientRect();
    const x = box.left + box.width / 2, y = box.top + box.height / 2;
    return box.width > 0 && x >= panel.left && x <= panel.right && y >= panel.top && y <= panel.bottom;
  });
}

/**
 * The side panel is not modal: the page behind it stays usable. Tab only has to pass over the
 * controls the panel hides, so they go inert for this one key press and nothing else changes:
 * the mouse, a screen reader's reading order and the panel's own focus order stay as they were.
 * @param {HTMLElement} dialog @param {HTMLElement} launcher
 */
function skipControlsUnderPanel(dialog, launcher) {
  const covered = controlsUnderPanel(dialog, launcher);
  for (const element of covered) element.inert = true;
  // A task after the key's default action, which has moved focus by then.
  setTimeout(() => {
    for (const element of covered) element.inert = false;
  });
}

/**
 * The background follows the panel's mode. Everything else on the page goes inert, the launcher
 * included (it is also hidden); only the panel and the live announcer stay available.
 * @param {HTMLElement} dialog @param {HTMLElement} launcher @param {boolean} on
 */
function applyModalState(dialog, launcher, on) {
  if (on) {
    dialog.setAttribute('role', 'dialog');
    dialog.setAttribute('aria-modal', 'true');
  } else {
    dialog.removeAttribute('role');
    dialog.removeAttribute('aria-modal');
  }
  launcher.style.visibility = on ? 'hidden' : '';
  for (const element of document.body.children) {
    const keep = element === dialog || element.id === 'assistant-announcer' || ['DIALOG', 'SCRIPT'].includes(element.tagName);
    if (!keep) /** @type {HTMLElement} */ (element).inert = on;
  }
}

/** @param {{launcher: HTMLElement, dialog: HTMLElement}} elements */
export function createChatPlacement({ launcher, dialog }) {
  const modalView = matchMedia(MODAL_VIEW);
  let isOpen = false;
  const modal = () => isOpen && modalView.matches;

  // The bubble's layout box: its hover/press scale must not shift the panel or the drag origin.
  function launcherBox() {
    const left = launcher.offsetLeft, top = launcher.offsetTop;
    return { left, top, right: left + launcher.offsetWidth, bottom: top + launcher.offsetHeight };
  }

  function placeDialog() {
    if (!isOpen) return;
    const view = viewportBox();
    const box = modal() ? modalPanel(view) : anchoredPanel(view, launcherBox());
    Object.assign(dialog.style, {
      width: `${box.width}px`, height: `${box.height}px`, left: `${box.left}px`, top: `${box.top}px`,
      right: 'auto', bottom: 'auto',
    });
    dialog.classList.toggle('is-compact', box.height < 400);
  }

  /** @param {number} x @param {number} y */
  function placeLauncher(x, y) {
    const view = viewportBox();
    const minX = view.left + LAUNCHER_EDGE, minY = view.top + LAUNCHER_EDGE;
    const maxX = Math.max(minX, minX + view.width - launcher.offsetWidth - LAUNCHER_EDGE * 2);
    const maxY = Math.max(minY, minY + view.height - launcher.offsetHeight - LAUNCHER_EDGE * 2);
    Object.assign(launcher.style, {
      left: `${Math.max(minX, Math.min(x, maxX))}px`, top: `${Math.max(minY, Math.min(y, maxY))}px`,
      right: 'auto', bottom: 'auto',
    });
    placeDialog();
  }

  function fitViewport() {
    const keyboardOpen = isOpen && (window.visualViewport?.height ?? Infinity) < window.innerHeight;
    if (modal() || !(drag.hasMoved() || keyboardOpen)) { placeDialog(); return; }
    drag.markMoved();
    const box = launcherBox();
    placeLauncher(box.left, box.top);
  }

  const applyModal = () => applyModalState(dialog, launcher, modal());

  const drag = setupLauncherDrag(launcher, { box: launcherBox, onMove: placeLauncher });
  modalView.addEventListener('change', () => {
    applyModal();
    placeDialog();
  });
  window.addEventListener('resize', fitViewport);
  window.visualViewport?.addEventListener('resize', fitViewport);
  window.visualViewport?.addEventListener('scroll', fitViewport);
  document.addEventListener('keydown', event => {
    if (event.key === 'Tab' && isOpen && !modalView.matches) skipControlsUnderPanel(dialog, launcher);
  }, true);

  return {
    consumeDragClick: drag.consumeClick,
    /** @param {boolean} open */
    setOpen(open) {
      isOpen = open;
      if (open) placeDialog();
      applyModal();
    },
  };
}
