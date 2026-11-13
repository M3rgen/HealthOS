// Streaming helpers for the Umay chat. They hold no DOM, so node tests cover them directly.

/**
 * One chat socket frame as an event, or null when the frame is not a JSON object with a type.
 * A broken frame is logged (its size only: the text may hold health data) and skipped, so the
 * connection and the answer around it carry on.
 */
export function parseFrame(/** @type {string} */ data, warn = console.warn) {
  let event = null;
  try {
    event = JSON.parse(data);
  } catch {
    /* reported below */
  }
  if (event && typeof event === 'object' && typeof event.type === 'string') return event;
  warn(`Umay: skipped an unreadable chat frame (${String(data ?? '').length} characters)`);
  return null;
}

/**
 * Collects the latest text per message and paints it at most once per animation frame, however
 * many deltas arrive in between. `drop(id)` forgets a pending paint (the final text is about to be
 * drawn anyway); `clear()` forgets all of them.
 */
/**
 * @param {(id: string, text: string) => void} paint
 * @param {{request?: (callback: FrameRequestCallback) => number, cancel?: (id: number) => void}} [scheduler]
 */
export function createFramePainter(paint, {
  request = callback => requestAnimationFrame(callback),
  cancel = id => cancelAnimationFrame(id),
} = {}) {
  /** @type {Map<string, string>} */
  const pending = new Map();
  /** @type {number | null} */
  let frame = null;
  function run() {
    frame = null;
    const batch = [...pending];
    pending.clear();
    for (const [id, text] of batch) paint(id, text);
  }
  function stop() {
    if (frame != null) cancel(frame);
    frame = null;
  }
  return {
    /** @param {string} id @param {string} text */
    queue(id, text) {
      pending.set(id, text);
      frame ??= request(run);
    },
    /** @param {string} id */
    drop(id) {
      pending.delete(id);
      if (!pending.size) stop();
    },
    clear() {
      pending.clear();
      stop();
    },
  };
}

/** A short, single-line version of an answer for the screen reader announcement. @param {unknown} text @param {number} [limit] */
export function summarize(text, limit = 160) {
  const plain = String(text ?? '').replace(/\s+/g, ' ').trim();
  if (plain.length <= limit) return plain;
  const cut = plain.slice(0, limit);
  const space = cut.lastIndexOf(' ');
  return `${(space > limit * 0.6 ? cut.slice(0, space) : cut).trimEnd()}…`;
}
