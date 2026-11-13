// Umay's socket: opened only once the panel is first opened, retried with a growing delay up to
// MAX_RETRIES, and resumed when the browser comes back online or the tab becomes visible.
import { parseFrame } from './chat-stream.js';
import { focusVisible } from './accessibility.js';
import { $ } from './dom.js';
import { t } from './i18n/index.js';

const MAX_RETRIES = 5;
const FIRST_RETRY_MS = 1000, LAST_RETRY_MS = 30000;

/** A growing retry delay and the single timer that waits it out. */
function createRetries() {
  /** @type {ReturnType<typeof setTimeout> | null} */
  let timer = null;
  let delay = FIRST_RETRY_MS, count = 0;
  const reset = () => { count = 0; delay = FIRST_RETRY_MS; };
  return {
    reset,
    cancel() {
      if (timer !== null) clearTimeout(timer);
      timer = null;
    },
    pending: () => timer !== null,
    exhausted: () => count >= MAX_RETRIES,
    /** @param {() => void} run */
    schedule(run) {
      count += 1;
      timer = setTimeout(run, delay);
      delay = Math.min(delay * 2, LAST_RETRY_MS);
    },
  };
}

/** @typedef {ReturnType<typeof import('./chat-messages.js').createChatMessages>} ChatMessages */
/** @typedef {{flags: {connected: boolean, busy: boolean, waiting: boolean}, paint: (text: string) => void, error: (text: string) => void}} ChatView */

function showNewChatBoundary() {
  const boundary = document.createElement('p');
  boundary.className = 'chat-boundary';
  boundary.setAttribute('role', 'status');
  boundary.textContent = t.umay.newChat;
  $('assistant-messages').insertBefore(boundary, $('assistant-typing'));
}

/** @typedef {{id: string, text: string, status: string, [field: string]: unknown}} ChatFrame */

/**
 * What each server frame does to the panel.
 * @param {{view: ChatView, messages: ChatMessages, onReady: () => void}} parts
 * @returns {Record<string, (event: ChatFrame) => void>}
 */
function chatEventHandlers({ view, messages, onReady }) {
  const { flags } = view;
  function answerArrived() {
    flags.waiting = false;
    $('assistant-typing').hidden = true;
  }

  return {
    ready() {
      if (!flags.connected && document.querySelector('.chat-turn')) showNewChatBoundary();
      flags.connected = true;
      onReady();
      view.error('');
      view.paint('');
    },
    working(/** @type {ChatFrame} */ event) {
      flags.busy = true;
      flags.waiting = true;
      view.paint(event.text);
      messages.scrollToEnd();
    },
    health_transfer: (/** @type {ChatFrame} */ event) => messages.updateTransfer(/** @type {import('./ui-types').TransferReceipt} */ (/** @type {unknown} */ (event))),
    delta(/** @type {ChatFrame} */ event) {
      answerArrived();
      messages.addDelta(event.id, event.text ?? '');
    },
    message(/** @type {ChatFrame} */ event) {
      answerArrived();
      messages.setFinal(event.id, event.text ?? '');
    },
    done(/** @type {ChatFrame} */ event) {
      messages.finishStreams();
      flags.busy = false;
      messages.announce(event.status);
      view.paint(event.status === 'interrupted' ? t.umay.stopped : event.status === 'failed' ? t.umay.failed : t.umay.ready);
    },
    error(/** @type {ChatFrame} */ event) {
      messages.finishStreams();
      view.error(event.text);
      flags.busy = false;
      view.paint(flags.connected ? t.umay.failed : t.umay.connectFailed);
    },
    notice: (/** @type {ChatFrame} */ event) => view.error(event.text),
  };
}

/**
 * Coming back online or to the tab skips the wait of a pending retry; it does not restart a stopped one.
 * @param {{online: () => void, visible: () => void, leaving: () => void, restored: () => void}} on
 */
function watchBrowser({ online, visible, leaving, restored }) {
  window.addEventListener('online', online);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) visible(); });
  window.addEventListener('pagehide', leaving);
  window.addEventListener('pageshow', event => { if (event.persisted) restored(); });
}

/**
 * The current socket. A replaced socket is silenced first, so its late events cannot touch the panel.
 * @param {{handlers: Record<string, (event: ChatFrame) => void>, onClose: () => void, onError: () => void}} parts
 */
function createChatSocket({ handlers, onClose, onError }) {
  /** @type {WebSocket | null} */
  let socket = null;
  return {
    closed: () => !socket || socket.readyState >= WebSocket.CLOSING,
    close() { socket?.close(); },
    /** @param {object} message */
    send(message) { socket?.send(JSON.stringify(message)); },
    discard() {
      if (!socket) return;
      socket.onclose = null;
      socket.onmessage = null;
      socket.onerror = null;
      socket.close();
    },
    open() {
      const next = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/api/codex/chat`);
      next.onmessage = ({ data }) => {
        const event = parseFrame(data);
        if (event && Object.hasOwn(handlers, event.type)) handlers[event.type](event);
      };
      next.onclose = onClose;
      next.onerror = onError;
      socket = next;
    },
  };
}

/** @param {{view: ChatView, messages: ChatMessages}} parts */
export function createChatConnection({ view, messages }) {
  const { flags } = view;
  const retries = createRetries();
  let started = false, pageLeaving = false;

  function goOffline() {
    flags.connected = false;
    flags.busy = false;
    flags.waiting = false;
    messages.disconnect();
  }
  const handlers = chatEventHandlers({ view, messages, onReady: retries.reset });
  const socket = createChatSocket({ handlers, onClose, onError: () => view.error(t.umay.socketError) });

  function onClose() {
    retries.cancel();
    goOffline();
    view.paint(t.umay.offline);
    $('assistant-reconnect').hidden = false;
    if (pageLeaving) return;
    if (retries.exhausted()) {
      view.error(t.umay.offlineLong);
      return;
    }
    if (!$('assistant-error').textContent) view.error(t.umay.retrying);
    retries.schedule(() => connect({ automatic: true }));
  }

  function connect({ automatic = false } = {}) {
    started = true;
    messages.loadFormatter();
    if (!automatic) retries.reset();
    retries.cancel();
    socket.discard();
    goOffline();
    view.error('');
    if (document.activeElement === $('assistant-reconnect')) focusVisible($('assistant-close'));
    $('assistant-reconnect').hidden = true;
    view.paint(t.umay.connecting);
    socket.open();
  }

  watchBrowser({
    online() { if (started && !flags.connected && !pageLeaving && socket.closed()) connect(); },
    visible() { if (started && !flags.connected && retries.pending() && socket.closed()) connect({ automatic: true }); },
    leaving() {
      pageLeaving = true;
      retries.cancel();
      socket.close();
    },
    restored() {
      pageLeaving = false;
      if (started) connect();
    },
  });

  return {
    connect,
    /** True while no socket is open or opening, so opening the panel may start one. */
    idle: () => !flags.connected && socket.closed(),
    send: socket.send,
  };
}
