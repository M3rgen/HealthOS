// A finished Umay answer as sanitized Markdown, drawn without losing the reader's place.
// Markdown + DOMPurify load with the first connection, not with the dashboard: the parser is
// most of the chat's weight and the panel is often never opened. An answer that finishes
// before the module arrives shows as plain text and is formatted as soon as it has loaded.
import { replaceWithFocus } from './accessibility.js';
import { $ } from './dom.js';
import { t } from './i18n/index.js';
import { updateScrollFocus } from './scroll-focus.js';

const FOCUSABLE_IN_ANSWER = 'a[href], pre, table';
const log = () => $('assistant-messages');

/** Code blocks and tables scroll on their own, so the keyboard must be able to reach them. @param {HTMLElement} content */
function labelBlocks(content) {
  content.querySelectorAll('pre, table').forEach((block, i) => {
    block.setAttribute('data-scroll-focus', '');
    if (block.tagName === 'PRE') block.setAttribute('role', 'region');
    block.setAttribute('aria-label', (block.tagName === 'PRE' ? t.umay.codeBlock : t.umay.table)(i + 1));
    updateScrollFocus(block);
  });
}

/** @param {WeakMap<HTMLElement, string>} latestText the text last drawn into each answer */
export function createAnswerFormatter(latestText) {
  /** @type {((text: string) => string) | null} */
  let formatAssistantMessage = null;
  /** @type {Promise<void> | null} */
  let loading = null;
  const load = () => (loading ??= import('./chat-format.js').then(
    module => { formatAssistantMessage = module.formatAssistantMessage; },
    () => { loading = null; },
  ));

  /** Called only once the formatter has loaded. @param {HTMLElement} content @param {string} text */
  function paint(content, text) {
    const reading = log().contains(document.activeElement);
    const focused = /** @type {HTMLElement | null} */ (document.activeElement);
    const index = [...content.querySelectorAll(FOCUSABLE_IN_ANSWER)].indexOf(/** @type {Element} */ (focused));
    const scroll = { left: focused?.scrollLeft ?? 0, top: focused?.scrollTop ?? 0 };
    replaceWithFocus(content, () => {
      content.innerHTML = /** @type {(text: string) => string} */ (formatAssistantMessage)(text);
      labelBlocks(content);
    }, (/** @type {Element} */ old) => {
      const next = content.querySelectorAll(FOCUSABLE_IN_ANSWER)[index];
      if (next?.tagName !== old.tagName || next.getAttribute('href') !== old.getAttribute('href')) return null;
      next.scrollLeft = scroll.left; next.scrollTop = scroll.top;
      return next;
    }, log());
    if (!reading) log().scrollTop = log().scrollHeight;
  }

  /** @param {HTMLElement} content @param {string} text */
  function show(content, text) {
    if (formatAssistantMessage) { paint(content, text); return; }
    content.textContent = text;
    load().then(() => {
      // Only if nothing newer was drawn into this answer meanwhile.
      if (formatAssistantMessage && latestText.get(content) === text && !content.classList.contains('is-streaming'))
        paint(content, text);
    });
  }

  return { load, show };
}
