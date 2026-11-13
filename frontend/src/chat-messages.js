// The chat log: one bubble per message, streamed answers shown as plain text and turned into
// sanitized Markdown once complete, with the reader's focus and scroll kept.
import { createFramePainter, summarize } from './chat-stream.js';
import { createAnswerFormatter } from './chat-markdown.js';
import { createTransfer } from './chat-transfer.js';
import { $ } from './dom.js';
import { t } from './i18n/index.js';

const log = () => $('assistant-messages');
const scrollToEnd = () => { log().scrollTop = log().scrollHeight; };
const logHasFocus = () => log().contains(document.activeElement);

/** @param {'user'|'assistant'} role @returns {{message: HTMLElement, content: HTMLElement}} */
function createBubble(role) {
  const message = document.createElement('article');
  message.className = `chat-message chat-${role}`;
  const label = document.createElement('span');
  label.className = 'chat-sender';
  label.textContent = role === 'user' ? t.umay.you : t.umay.name;
  const content = document.createElement('div');
  content.className = 'chat-content';
  message.append(label, content);
  return { message, content };
}

// A short summary in a separate polite live region: the log itself stays quiet while streaming.
/** @param {string} status @param {string | null | undefined} answer */
function announceAnswer(status, answer) {
  let text = '';
  if (status === 'interrupted') text = t.umay.interrupted;
  else if (status === 'completed' && answer?.trim()) text = `${t.umay.answered}: ${summarize(answer)}`;
  if (text) $('assistant-announcer').textContent = text;
}

/**
 * Streamed text per answer id. While an answer streams it is shown as plain text, painted at
 * most once per frame; when its final text arrives (or the turn ends) it becomes Markdown.
 * @param {{paintPlain: (id: string, text: string) => void, paintFinal: (id: string, text: string) => void}} paints
 */
function createAnswerStream({ paintPlain, paintFinal }) {
  /** @type {Map<string, string>} */
  const texts = new Map();
  /** @type {Set<string>} */
  const streaming = new Set();
  /** @type {string | null} */
  let last = null;
  const painter = createFramePainter(paintPlain);

  // Turns every answer that is still plain streamed text into its final Markdown.
  function finish() {
    for (const id of streaming) {
      painter.drop(id);
      paintFinal(id, texts.get(id) ?? '');
    }
    streaming.clear();
  }

  return {
    finish,
    lastId: () => last,
    /** Drops pending paints without drawing them: the whole log is about to go. */
    discard() {
      painter.clear();
      streaming.clear();
    },
    forget() {
      finish();
      painter.clear();
      texts.clear();
      last = null;
    },
    /** @param {string} id @param {string} delta */
    addDelta(id, delta) {
      const text = (texts.get(id) ?? '') + delta;
      // An answer already shown as Markdown stays Markdown (focus inside it is kept).
      const final = texts.has(id) && !streaming.has(id);
      texts.set(id, text);
      last = id;
      if (final) paintFinal(id, text);
      else {
        streaming.add(id);
        painter.queue(id, text);
      }
    },
    /** @param {string} id @param {string} text */
    setFinal(id, text) {
      painter.drop(id);
      streaming.delete(id);
      texts.set(id, text);
      last = id;
      paintFinal(id, text);
    },
  };
}

/** A new section for one turn's messages, above the typing indicator. */
function openTurnSection() {
  const section = document.createElement('section');
  section.className = 'chat-turn';
  log().insertBefore(section, $('assistant-typing'));
  return section;
}

export function createChatMessages() {
  /** @type {Map<string, HTMLElement>} */
  const items = new Map();
  /** @type {WeakMap<HTMLElement, string>} */
  const latestText = new WeakMap();
  /** @type {HTMLElement | null} */
  let turnContainer = null;
  /** @type {ReturnType<typeof createTransfer> | null} */
  let transfer = null;
  const answers = createAnswerFormatter(latestText);
  const stream = createAnswerStream({
    paintPlain: (id, text) => append('assistant', text, id, { plain: true }),
    paintFinal: (id, text) => append('assistant', text, id),
  });

  /** @param {'user'|'assistant'} role @param {string} [id] */
  function contentFor(role, id) {
    const existing = id ? items.get(id) : undefined;
    if (existing) return existing;
    const { message, content } = createBubble(role);
    if (turnContainer) turnContainer.append(message);
    else log().insertBefore(message, $('assistant-typing'));
    if (id) items.set(id, content);
    return content;
  }

  /** @param {'user'|'assistant'} role @param {string} text @param {string} [id] @param {{plain?: boolean}} [options] */
  function append(role, text, id, { plain = false } = {}) {
    const reading = logHasFocus();
    const content = contentFor(role, id);
    content.classList.toggle('is-streaming', plain);
    latestText.set(content, text);
    if (role === 'assistant' && !plain) answers.show(content, text);
    else content.textContent = text;
    if (!reading) scrollToEnd();
  }

  function forgetAnswers() {
    stream.forget();
    items.clear();
  }

  return {
    loadFormatter: answers.load,
    append,
    finishStreams: stream.finish,
    /** A connection ended: the running turn's parts stop listening. */
    disconnect() {
      forgetAnswers();
      transfer?.disconnect();
      turnContainer = null;
      transfer = null;
    },
    beginTurn() {
      forgetAnswers();
      turnContainer = openTurnSection();
      transfer = createTransfer(turnContainer);
    },
    /** @param {import('./ui-types').TransferReceipt} event */
    updateTransfer(event) { transfer?.update(event); },
    addDelta: stream.addDelta,
    setFinal: stream.setFinal,
    /** @param {string} status */
    announce(status) { announceAnswer(status, stream.lastId() && items.get(/** @type {string} */ (stream.lastId()))?.textContent); },
    clearAll() {
      stream.discard();
      document.querySelectorAll('.chat-turn, .chat-message, .chat-boundary').forEach(message => message.remove());
      items.clear();
    },
    scrollToEnd,
  };
}
