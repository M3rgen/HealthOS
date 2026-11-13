// Updates an element's children in place from fresh markup. A poll repaints the same data every
// 60 seconds; rewriting innerHTML threw away the list's scroll position, open <details>, expanded
// rows and the focused control. Existing nodes survive here and only what differs is written.
//   data-key      names a child that keeps its node when its siblings change (else: the id)
//   data-keep     space-separated attributes the user owns; a repaint leaves them as they are
// <details open> is always the user's. morphChildren is an HTML sink: eslint.config.js holds its
// markup argument to the same rule as innerHTML.

const ELEMENT = 1;

// Private to this sink: only morphChildren's lint-checked argument can create this wrapper.
// This is a trust boundary, not a sanitizer; callers must use an escaped markup builder.
class SafeHtml {
  /** @type {string} */
  #escapedMarkup;
  /** @param {string} escapedMarkup */
  constructor(escapedMarkup) { this.#escapedMarkup = escapedMarkup; }
  unwrap() { return this.#escapedMarkup; }
}

/** @param {Node} node @returns {string | null} */
function keyOf(node) {
  if (node.nodeType !== ELEMENT) return null;
  const element = /** @type {Element} */ (node);
  return element.getAttribute('data-key') ?? (element.id || null);
}

/** @param {Node} a @param {Node} b */
const sameKind = (a, b) => a.nodeType === b.nodeType && a.nodeName === b.nodeName && keyOf(a) === keyOf(b);

/** @param {Element} element */
function userOwned(element) {
  const owned = (element.getAttribute('data-keep') ?? '').split(/\s+/).filter(Boolean);
  if (element.localName === 'details') owned.push('open');
  return owned;
}

/** @param {Element} current @param {Element} next */
function syncAttributes(current, next) {
  const owned = userOwned(next);
  for (const name of current.getAttributeNames())
    if (!next.hasAttribute(name) && !owned.includes(name)) current.removeAttribute(name);
  for (const name of next.getAttributeNames()) {
    const value = /** @type {string} */ (next.getAttribute(name));
    if (!owned.includes(name) && current.getAttribute(name) !== value) current.setAttribute(name, value);
  }
}

/** @param {Node} current @param {Node} next */
function updateNode(current, next) {
  if (current.nodeType !== ELEMENT) {
    if (current.nodeValue !== next.nodeValue) current.nodeValue = next.nodeValue;
    return;
  }
  syncAttributes(/** @type {Element} */ (current), /** @type {Element} */ (next));
  reconcile(current, next);
}

/** Makes `parent`'s children match `next`'s, reusing keyed nodes wherever they now stand. @param {Node} parent @param {Node} next */
function reconcile(parent, next) {
  /** @type {Map<string, ChildNode>} */
  const keyed = new Map();
  for (const child of parent.childNodes) {
    const key = keyOf(child);
    if (key !== null) keyed.set(key, child);
  }
  let cursor = parent.firstChild;
  for (const incoming of [...next.childNodes]) {
    const key = keyOf(incoming);
    const match = key === null ? cursor : keyed.get(key);
    if (!match || !sameKind(match, incoming)) {
      parent.insertBefore(incoming, cursor);
      continue;
    }
    if (key !== null) keyed.delete(key);
    if (match === cursor) cursor = cursor.nextSibling;
    else parent.insertBefore(match, cursor);
    updateNode(match, incoming);
  }
  while (cursor) {
    const after = cursor.nextSibling;
    cursor.remove();
    cursor = after;
  }
}

/** @param {SafeHtml} safeHtml @returns {DocumentFragment} */
function parse(safeHtml) {
  const template = document.createElement('template');
  template.innerHTML = safeHtml.unwrap();
  return template.content;
}

/**
 * @param {Element} element
 * @param {string | DocumentFragment} source markup built with escape(), or ready nodes
 */
export function morphChildren(element, source) {
  const safeHtml = typeof source === 'string' ? new SafeHtml(source) : source;
  if (!element.firstChild) {
    if (safeHtml instanceof SafeHtml) element.innerHTML = safeHtml.unwrap();
    else element.append(...safeHtml.childNodes);
    return;
  }
  const focused = document.activeElement;
  reconcile(element, safeHtml instanceof SafeHtml ? parse(safeHtml) : safeHtml);
  // Moving a keyed node blurs it; a control that is still there keeps the keyboard.
  if (focused instanceof HTMLElement && focused.isConnected && focused !== document.activeElement) focused.focus({ preventScroll: true });
}
