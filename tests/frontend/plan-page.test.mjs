import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire, registerHooks } from 'node:module';
import { civilDate, civilParts, shiftDay } from '../../frontend/src/dates.js';
import { number } from '../../frontend/src/locale.js';

// plan.js imports its stylesheet; Node has no CSS loader, so only that import is stubbed.
const hooks = registerHooks({
  load(url, context, next) {
    if (url.endsWith('/frontend/src/plan.css')) return { format: 'module', source: '', shortCircuit: true };
    return next(url, context);
  },
});
const { setupPlan } = await import('../../frontend/src/plan.js');
const format = { civilDate, civilParts, shiftDay, number };
hooks.deregister();

const require = createRequire(import.meta.url);
const fixture = require('../../frontend/preview/fixtures.cjs').plan;

// Minimal DOM boundary for request ordering and form behavior, not a substitute for layout checks.
class Element {
  children = []; attributes = new Map(); dataset = {}; value = ''; checked = false;
  hidden = false; id = ''; className = ''; validity = { valid: true }; ownText = '';
  constructor(tag) { this.tagName = tag; }
  set textContent(text) { this.ownText = text; this.children = []; }
  get textContent() { return this.ownText + this.children.map(node => node.textContent).join(''); }
  get firstElementChild() { return this.children[0] ?? null; }
  append(...children) { children.forEach(child => { child.parentNode = this; }); this.children.push(...children); }
  remove() { this.parentNode.children.splice(this.parentNode.children.indexOf(this), 1); this.parentNode = null; }
  replaceChildren(...children) { this.ownText = ''; this.children = []; this.append(...children); }
  setAttribute(name, value) { this.attributes.set(name, value); }
  getAttribute(name) { return this.attributes.get(name) ?? null; }
  removeAttribute(name) { this.attributes.delete(name); }
  get classList() { return {
    add: name => { this.className += ` ${name}`; },
    toggle: (name, on) => { this.className = this.className.split(' ').filter(item => item !== name).concat(on ? [name] : []).join(' '); },
  }; }
  focus() { document.activeElement = this; }
  click() { return this.onclick?.(); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] ?? null; }
  querySelectorAll(selector) {
    if (selector.includes(' ')) {
      const [parent, child] = selector.split(' ');
      return this.querySelectorAll(parent).flatMap(node => node.querySelectorAll(child));
    }
    return this.children.flatMap(node => [...(node.matches(selector) ? [node] : []), ...node.querySelectorAll(selector)]);
  }
  matches(selector) {
    if (selector.startsWith('#')) return this.id === selector.slice(1);
    if (selector.startsWith('.')) return this.className.split(' ').includes(selector.slice(1));
    if (selector === 'input[data-item-id]') return this.tagName === 'input' && this.dataset.itemId !== undefined;
    if (selector.startsWith('[')) return this.attributes.has(selector.slice(1, -1));
    return this.tagName === selector;
  }
}
function setup(t) {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const root = new Element('section'), requests = [], notices = [];
  let today = '2026-10-16', measured = 0;
  const measure = { click: () => measured++ };
  const doc = { documentElement: { lang: 'tr' }, activeElement: null, createElement: tag => new Element(tag),
    getElementById: id => id === 'plan' ? root : id === 'measurements-add' ? measure : root.querySelector(`#${id}`) };
  const previous = globalThis.document;
  globalThis.document = doc;
  t.after(() => { if (previous === undefined) delete globalThis.document; else globalThis.document = previous; });
  const controller = setupPlan({ notice: (...args) => notices.push(args), getToday: () => today, format,
    request: (url, options) => new Promise((resolve, reject) => requests.push({ url, options, resolve, reject })),
  });
  return { root, controller, requests, notices, measured: () => measured, today: value => { today = value; } };
}
async function load(view, response = fixture) {
  const task = view.controller.load();
  view.requests.at(-1).resolve(structuredClone(response));
  await task;
}
function select(view, key) {
  view.root.querySelectorAll('button').find(node => node.dataset.section === key).click();
}
const tick = async t => { t.mock.timers.tick(120); await Promise.resolve(); };

test('empty and sparse plans render their own state and reuse the existing measurement flow', async t => {
  const view = setup(t);
  await load(view, { plan: null });
  assert.match(view.root.textContent, /docs\/reference\/plan.md/);
  assert.match(view.root.querySelector('code').textContent, /app.cli.plan_import/);
  assert.equal(view.root.querySelector('nav').children.length, 0);
  await load(view, { version: 2, plan: { schema: 1, measurements: ['Synthetic measurement instruction'] } });
  assert.deepEqual(view.root.querySelector('nav').children.map(node => node.dataset.section), ['measurements']);
  view.root.querySelector('.plan-content').querySelector('button').click();
  assert.equal(view.measured(), 1);
  await load(view, { version: 3, plan: { schema: 1 } });
  assert.equal(view.root.querySelector('nav').children.length, 0);
  assert.match(view.root.textContent, /gösterilecek bölüm yok/);
});

test('a failed date save rolls back the field and dependent week without losing keyboard focus', async t => {
  const view = setup(t);
  await load(view);
  let input = view.root.querySelector('#plan-start');
  input.focus();
  input.value = '2026-10-01'; input.onchange();
  assert.equal(view.root.querySelector('#plan-start').value, '2026-10-01');
  assert.match(view.root.querySelector('.plan-week').textContent, /3/);
  assert.equal(document.activeElement.id, 'plan-start');
  await tick(t);
  const write = view.requests.at(-1);
  assert.equal(write.options.method, 'PUT');
  assert.equal(write.options.headers['X-HealthOS-Request'], '1');
  write.reject(new Error('Synthetic failure')); await Promise.resolve();
  input = view.root.querySelector('#plan-start');
  assert.equal(input.value, fixture.state.start_date);
  assert.match(view.root.querySelector('.plan-week').textContent, /1/);
  assert.equal(view.notices.length, 1);
  assert.equal(document.activeElement, input);
});

test('an older GET cannot overwrite a local edit whose PUT has already completed', async t => {
  const view = setup(t);
  await load(view); select(view, 'shopping');
  const oldRead = view.controller.load(), stale = view.requests.at(-1);
  const input = view.root.querySelectorAll('input[data-item-id]')[0];
  input.checked = true; input.onchange();
  await tick(t);
  const write = view.requests.at(-1), saved = JSON.parse(write.options.body);
  write.resolve(saved); await Promise.resolve();
  stale.resolve(structuredClone(fixture)); await oldRead;
  assert.equal(input.checked, true);
  assert.equal(view.controller.actions.state.checked_item_ids[0], input.dataset.itemId);
  await load(view); // A later authoritative read can update another device's changes.
  assert.equal(input.checked, false);
});

test('a refresh advances Today across midnight and leaves an explicitly selected date alone', async t => {
  const view = setup(t);
  await load(view);
  view.today('2026-10-17'); await load(view);
  assert.equal(view.root.querySelector('#plan-day').value, '2026-10-17');
  assert.match(view.root.textContent, /öğün tanımlanmamış/);
  let input = view.root.querySelector('#plan-day');
  input.value = '2026-10-12'; input.onchange();
  assert.match(view.root.textContent, /Örnek kase/);
  const toggle = view.root.querySelector('.plan-meal').querySelector('button');
  toggle.click();
  assert.equal(toggle.getAttribute('aria-expanded'), 'true');
  assert.equal(view.root.querySelector(`#${toggle.getAttribute('aria-controls')}`).hidden, false);
  toggle.click();
  assert.equal(toggle.getAttribute('aria-expanded'), 'false');
  view.today('2026-10-18'); await load(view);
  input = view.root.querySelector('#plan-day');
  assert.equal(input.value, '2026-10-12');
  input.value = ''; input.onchange(); // A cleared field returns to today.
  assert.equal(view.root.querySelector('#plan-day').value, '2026-10-18');
});


test('Training highlights the actual current week after another day is selected in Today', async t => {
  const view = setup(t);
  await load(view);
  const input = view.root.querySelector('#plan-day');
  input.value = '2026-09-01'; input.onchange();
  select(view, 'training');
  const visibleRows = view.root.querySelectorAll('.plan-current').filter(row => {
    for (let node = row; node; node = node.parentNode) if (node.hidden) return false;
    return true;
  });
  assert.equal(visibleRows.length, 1);
  assert.match(visibleRows[0].textContent, /İçinde bulunulan hafta/);
});


test('another device changing the start date preserves row focus and shopping controls', async t => {
  const view = setup(t);
  await load(view);
  view.root.querySelector('.plan-meal').querySelector('button').focus();
  const remote = structuredClone(fixture);
  remote.state.start_date = '2026-10-01';
  await load(view, remote);
  assert.equal(document.activeElement, view.root.querySelector('.plan-meal').querySelector('button'));
  select(view, 'shopping');
  const checkbox = view.root.querySelectorAll('input[data-item-id]')[0];
  checkbox.focus();
  remote.state.start_date = '2026-10-02';
  await load(view, remote);
  assert.equal(document.activeElement, checkbox);
  assert.equal(view.root.querySelectorAll('input[data-item-id]')[0], checkbox);
});
