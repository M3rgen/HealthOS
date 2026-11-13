// The Umay panel: composes the bubble's placement, the message log and the socket, and owns the
// panel's status line and controls.
import { focusVisible } from './accessibility.js';
import { createChatConnection } from './chat-connection.js';
import { createChatMessages } from './chat-messages.js';
import { createChatPlacement } from './chat-placement.js';
import { $ } from './dom.js';
import { t } from './i18n/index.js';

/** The status line while connected, or the given offline text. @param {{connected: boolean, busy: boolean}} flags @param {string} text */
const statusText = ({ connected, busy }, text) => {
  if (!connected) return text;
  return busy ? t.umay.typing : t.umay.active;
};

/** @param {{connected: boolean, busy: boolean, waiting: boolean}} flags */
function paintControls({ connected, busy, waiting }) {
  $('assistant-status').dataset.presence = connected ? 'online' : 'offline';
  $('assistant-typing').hidden = !connected || !busy || !waiting;
  $('assistant-send').disabled = !connected || busy;
  $('assistant-input').disabled = !connected;
  $('assistant-stop').hidden = !busy;
  $('assistant-new').disabled = busy;
  $('assistant-messages').setAttribute('aria-busy', String(busy));
}

/** @param {HTMLElement} dialog */
function createChatView(dialog) {
  const flags = { connected: false, busy: false, waiting: false };
  return {
    flags,
    /** @param {string} text the status shown while not connected */
    paint(text) {
      const focused = /** @type {HTMLButtonElement | null} */ (document.activeElement);
      $('assistant-status').textContent = statusText(flags, text);
      paintControls(flags);
      if (dialog.contains(focused) && (focused?.disabled || focused?.hidden)) focusVisible($('assistant-close'));
    },
    /** @param {string} text */
    error(text) {
      if (!text && document.activeElement === $('assistant-error')) focusVisible($('assistant-close'));
      $('assistant-error').textContent = text;
      $('assistant-error').hidden = !text;
    },
  };
}

/** The composer sends on submit and on Enter; Shift+Enter and an open IME keep typing. @param {(text: string) => void} send */
function wireComposer(send) {
  $('assistant-form').addEventListener('submit', event => {
    event.preventDefault();
    send($('assistant-input').value);
  });
  $('assistant-input').addEventListener('keydown', event => {
    if (event.key !== 'Enter' || event.shiftKey || event.isComposing) return;
    event.preventDefault();
    send($('assistant-input').value);
  });
}

/** @param {() => string} getTimezone */
export function setupChat(getTimezone) {
  const launcher = $('assistant-open');
  const dialog = $('assistant-dialog');
  const view = createChatView(dialog);
  const messages = createChatMessages();
  const placement = createChatPlacement({ launcher, dialog });
  const connection = createChatConnection({ view, messages });
  const { flags } = view;

  function reset() {
    messages.clearAll();
    $('assistant-input').value = '';
    connection.connect();
  }

  /** @param {string} text */
  function send(text) {
    if (!flags.connected || flags.busy || !text.trim()) return;
    view.error('');
    messages.beginTurn();
    messages.append('user', text.trim());
    connection.send({ text: text.trim(), timezone: getTimezone() });
    $('assistant-input').value = '';
    flags.busy = true;
    flags.waiting = true;
    view.paint(t.umay.thinking);
    messages.scrollToEnd();
  }

  /** @param {boolean} open */
  function setOpen(open) {
    dialog.hidden = !open;
    placement.setOpen(open);
    launcher.setAttribute('aria-expanded', String(open));
  }

  launcher.addEventListener('click', event => {
    if (placement.consumeDragClick(event)) return;
    setOpen(true);
    if (connection.idle()) connection.connect();
    focusVisible(flags.connected ? $('assistant-input') : $('assistant-close'));
  });
  $('assistant-close').addEventListener('click', () => {
    setOpen(false);
    launcher.focus();
  });
  dialog.addEventListener('keydown', event => {
    if (event.key !== 'Escape' || event.defaultPrevented || document.querySelector(':popover-open')) return;
    event.preventDefault();
    event.stopPropagation();
    $('assistant-close').click();
  });
  wireComposer(send);
  $('assistant-stop').addEventListener('click', () => {
    if (flags.connected && flags.busy) connection.send({ type: 'cancel' });
  });
  $('assistant-new').addEventListener('click', reset);
  $('assistant-reconnect').addEventListener('click', () => connection.connect());
}
