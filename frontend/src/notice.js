import { t } from './i18n/index.js';

// How long a non-error notice stays before it leaves on its own.
export const NOTICE_TIMEOUT_MS = 5000;

/**
 * The page's single status box. An error stays until it is closed or the same source succeeds
 * (`clear(source)`); every other notice also leaves on its own after NOTICE_TIMEOUT_MS.
 * `detail` (raw error codes) goes into the tooltip for debugging; the text stays readable.
 * `action` ({label, run}) shows a retry button beside the text; a notice without one hides it.
 */
/** @typedef {{label: string, run: () => unknown}} NoticeAction */
/**
 * @param {{box: HTMLElement, text: HTMLElement, close: HTMLElement, action?: HTMLButtonElement | null,
 *   onClosed?: () => void, timers?: Pick<typeof globalThis, 'setTimeout'|'clearTimeout'>}} parts
 */
export function createNotice({ box, text, close, action = null, onClosed = () => {}, timers = globalThis }) {
  /** @type {ReturnType<typeof setTimeout> | null} */
  let timer = null;
  /** @type {(() => unknown) | null} */
  let run = null;
  close.setAttribute('aria-label', t.notice.close);
  close.title = t.notice.close;
  if (action) {
    close.before(action);
    action.addEventListener('click', () => run?.());
  }

  function hide() {
    if (timer !== null) timers.clearTimeout(timer);
    timer = null;
    const focused = box.contains(document.activeElement);
    box.hidden = true;
    box.classList.remove('error');
    delete box.dataset.source;
    text.textContent = '';
    text.title = '';
    paintAction(null);
    if (focused) onClosed();
  }

  /** @param {NoticeAction | null} next */
  function paintAction(next) {
    run = next?.run ?? null;
    if (!action) return;
    action.hidden = !next;
    action.textContent = next?.label ?? '';
  }

  /** @param {string} message @param {{error?: boolean, source?: string, detail?: string, action?: NoticeAction | null}} [options] */
  function show(message, { error = false, source = 'general', detail = '', action: next = null } = {}) {
    if (timer !== null) timers.clearTimeout(timer);
    timer = null;
    box.hidden = false;
    box.classList.toggle('error', error);
    box.dataset.source = source;
    text.textContent = message;
    text.title = detail;
    paintAction(next);
    if (!error) timer = timers.setTimeout(() => {
      if (box.dataset.source === source && text.textContent === message) hide();
    }, NOTICE_TIMEOUT_MS);
  }

  /** Hides the notice when one of `sources` showed it; a notice from another source stays. @param {...string} sources */
  function clear(...sources) {
    if (!box.hidden && sources.includes(/** @type {string} */ (box.dataset.source))) hide();
  }

  close.addEventListener('click', hide);
  return { show, clear, hide, get source() { return box.hidden ? null : box.dataset.source; } };
}
