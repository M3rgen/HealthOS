import assert from 'node:assert/strict';
import test from 'node:test';
import { setupScrollFocus, updateScrollFocus } from '../../frontend/src/scroll-focus.js';

// Geometry is supplied at the controller boundary; native Tab/arrow keys and actual
// layout are covered by tests/browser/scroll-focus.cjs.
class Scrollport {
  clientWidth = 200; clientHeight = 100; scrollWidth = 200; scrollHeight = 100;
  isConnected = true;
  attrs = new Map([['role', 'region'], ['aria-label', 'Sentetik kayıtlar']]);
  getAttribute(name) { return this.attrs.get(name) ?? null; }
  setAttribute(name, value) { this.attrs.set(name, value); }
  removeAttribute(name) { this.attrs.delete(name); }
}

test('fitting, horizontal, vertical and hidden scrollports have only the required Tab stops', () => {
  const el = new Scrollport();
  updateScrollFocus(el);
  assert.equal(el.getAttribute('tabindex'), null);
  assert.equal(el.getAttribute('role'), null);
  assert.equal(el.getAttribute('aria-label'), null);
  for (const axis of ['scrollWidth', 'scrollHeight']) {
    el[axis]++;
    updateScrollFocus(el);
    assert.equal(el.getAttribute('tabindex'), '0', 'one overflowing pixel is sufficient');
    assert.equal(el.getAttribute('role'), 'region');
    assert.equal(el.getAttribute('aria-label'), 'Sentetik kayıtlar');
    el[axis]--;
    updateScrollFocus(el);
    assert.equal(el.getAttribute('tabindex'), null);
    assert.equal(el.getAttribute('aria-label'), null);
  }
  el.clientWidth = 0; el.clientHeight = 0;
  updateScrollFocus(el);
  assert.equal(el.getAttribute('tabindex'), null, 'hidden containers do not get a scroll stop');
  el.clientWidth = 100; el.clientHeight = 100;
  updateScrollFocus(el);
  assert.equal(el.getAttribute('tabindex'), '0', 'showing overflowing content restores the stop');
});

test('live regions, tabpanels, named forms and tables retain their independent semantics', () => {
  for (const role of ['status', 'alert', 'log', 'group', 'tabpanel', 'table', null]) {
    const el = new Scrollport();
    if (role) el.setAttribute('role', role); else el.removeAttribute('role');
    el.setAttribute('aria-live', 'polite');
    updateScrollFocus(el);
    assert.equal(el.getAttribute('tabindex'), null);
    assert.equal(el.getAttribute('role'), role);
    assert.equal(el.getAttribute('aria-label'), 'Sentetik kayıtlar');
    assert.equal(el.getAttribute('aria-live'), 'polite');
  }
});

test('resize and content observers update unchanged boxes, discover new content and release removed blocks', async t => {
  const originals = new Map(['document', 'ResizeObserver', 'MutationObserver', 'requestAnimationFrame']
    .map(name => [name, Object.getOwnPropertyDescriptor(globalThis, name)]));
  t.after(() => {
    for (const [name, descriptor] of originals) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor);
      else delete globalThis[name];
    }
  });
  const el = new Scrollport(), blocks = [el], frames = [], watched = new Set();
  let resize, mutate, mutationOptions;
  globalThis.document = {body: {}, querySelectorAll: () => blocks, fonts: {ready: Promise.resolve()}};
  globalThis.requestAnimationFrame = callback => frames.push(callback);
  globalThis.ResizeObserver = class {
    constructor(callback) { resize = callback; }
    observe(element) { watched.add(element); }
    unobserve(element) { watched.delete(element); }
  };
  globalThis.MutationObserver = class {
    constructor(callback) { mutate = callback; }
    observe(root, options) { mutationOptions = options; }
  };
  const paint = () => frames.splice(0).forEach(callback => callback());
  setupScrollFocus();
  await Promise.resolve(); paint(); // Font readiness also requests a measurement.
  assert.equal(el.getAttribute('tabindex'), null);
  el.clientWidth = 100;
  resize(); paint();
  assert.equal(el.getAttribute('tabindex'), '0');
  el.scrollWidth = 100;
  mutate(); paint();
  assert.equal(el.getAttribute('tabindex'), null, 'a redraw need not resize the container');
  const dynamic = new Scrollport(); dynamic.scrollHeight = 300; blocks.push(dynamic);
  mutate(); resize();
  assert.equal(frames.length, 1, 'content and resize notifications share one scheduled measurement');
  paint();
  assert.equal(dynamic.getAttribute('tabindex'), '0');
  assert(watched.has(dynamic));
  dynamic.isConnected = false; blocks.pop();
  mutate(); paint();
  assert(!watched.has(dynamic), 'removed Markdown/transfer blocks do not retain resize observers');
  assert(mutationOptions.childList && mutationOptions.characterData && mutationOptions.subtree);
  for (const attribute of ['hidden', 'open', 'class', 'style']) assert(mutationOptions.attributeFilter.includes(attribute));
  for (const attribute of ['tabindex', 'role', 'aria-label']) assert(!mutationOptions.attributeFilter.includes(attribute));
});
