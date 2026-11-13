// Scroll-only Tab stops and region names follow the rendered content, including hidden
// panels, async table redraws and details opened later. Live-region announcements stay intact.
/** @type {WeakMap<Element, Map<string, string>>} */
const attributes = new WeakMap();

/** @param {Element} element */
function scrollAttributes(element) {
  const saved = new Map();
  // A group/tabpanel, form, table or live region has meaning even without scrolling.
  if (element.getAttribute('role') === 'region') {
    for (const name of ['role', 'aria-label']) {
      const value = element.getAttribute(name);
      if (value !== null) saved.set(name, value);
    }
  }
  return saved;
}

/** Also used before restoring focus to a freshly rendered Markdown block. @param {Element} element */
export function updateScrollFocus(element) {
  if (!attributes.has(element)) attributes.set(element, scrollAttributes(element));
  const overflowing = element.clientWidth > 0 && element.clientHeight > 0 &&
    (element.scrollWidth > element.clientWidth || element.scrollHeight > element.clientHeight);
  if (overflowing) element.setAttribute('tabindex', '0');
  else element.removeAttribute('tabindex');
  for (const [name, value] of /** @type {Map<string, string>} */ (attributes.get(element))) {
    if (overflowing) element.setAttribute(name, value);
    else element.removeAttribute(name);
  }
}

export function setupScrollFocus() {
  /** @type {Set<Element>} */
  const observed = new Set();
  let pending = false;
  const resize = new ResizeObserver(schedule);

  function update() {
    pending = false;
    for (const element of observed) {
      if (element.isConnected) continue;
      resize.unobserve(element);
      observed.delete(element);
    }
    for (const element of document.querySelectorAll('[data-scroll-focus]')) {
      if (!observed.has(element)) {
        observed.add(element);
        resize.observe(element);
      }
      updateScrollFocus(element);
    }
  }

  function schedule() {
    if (pending) return;
    pending = true;
    requestAnimationFrame(update);
  }

  // Observe content as well as box sizes: replacing rows need not resize the scrollport.
  // Our own tabindex/role/label writes are excluded, so they cannot feed back into this.
  new MutationObserver(schedule).observe(document.body, {
    subtree: true, childList: true, characterData: true, attributes: true,
    attributeFilter: ['hidden', 'open', 'class', 'style'],
  });
  document.fonts.ready.then(schedule);
  update();
}
