import test from 'node:test';
import assert from 'node:assert/strict';
import { setupBodyLoader } from '../../frontend/src/body-loader.js';

function environment(t, { observer = true, narrow = false, load } = {}) {
  const globals = ['document', 'window', 'matchMedia', 'IntersectionObserver', 'requestAnimationFrame', 'cancelAnimationFrame', 'setTimeout', 'clearTimeout'];
  const saved = globals.map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]);
  t.after(() => saved.forEach(([key, descriptor]) => {
    if (descriptor) Object.defineProperty(globalThis, key, descriptor);
    else delete globalThis[key];
  }));
  const events = () => ({
    listeners: new Map(),
    addEventListener(type, callback) { this.listeners.set(type, callback); },
    removeEventListener(type) { this.listeners.delete(type); },
    emit(type) { this.listeners.get(type)?.(); },
  });
  const doc = { ...events(), hidden: false };
  const win = events(), reduced = { ...events(), matches: false };
  const frames = new Map(), timers = new Map();
  let serial = 0, notify, connected = false, calls = 0, enabled = true, visible = true;
  const container = { dataset: {}, getClientRects: () => visible ? [{}] : [] };
  const applied = { metrics: [], motion: [], disposed: 0 };
  const createBody = (element, motion) => {
    assert.equal(element, container);
    calls++;
    applied.motion.push(motion);
    return {
      setBodyMetrics: value => applied.metrics.push(value),
      setMotion: value => applied.motion.push(value),
      dispose: () => applied.disposed++,
    };
  };
  let imports = 0;
  const values = {
    document: doc, window: win, matchMedia: query => query.includes('max-width') ? { matches: narrow } : reduced,
    requestAnimationFrame(callback) { frames.set(++serial, callback); return serial; },
    cancelAnimationFrame(id) { frames.delete(id); },
    setTimeout(callback, ms) { timers.set(++serial, { callback, ms }); return serial; },
    clearTimeout(id) { timers.delete(id); },
    IntersectionObserver: observer ? class {
      constructor(callback) { notify = callback; }
      observe(element) { assert.equal(element, container); connected = true; }
      disconnect() { connected = false; }
    } : undefined,
  };
  Object.entries(values).forEach(([key, value]) => Object.defineProperty(globalThis, key, { value, configurable: true, writable: true }));
  const controller = setupBodyLoader(container, {
    getMotion: () => enabled,
    load: () => { imports++; return load ? load(createBody) : Promise.resolve({ createBody }); },
  });
  const tick = () => {
    const pending = [...frames.values()]; frames.clear(); pending.forEach(callback => callback());
  };
  return {
    controller, container, doc, win, reduced, applied, frames, timers, tick,
    painted() { tick(); tick(); },
    intersect(value = true) { notify?.([{ isIntersecting: value }]); },
    fallback() { const pending = [...timers.values()]; timers.clear(); pending.forEach(({ callback }) => callback()); },
    setMotion(value) { enabled = value; controller.updateMotion(); },
    setVisible(value) { visible = value; controller.updateMotion(); },
    get imports() { return imports; }, get calls() { return calls; }, get connected() { return connected; },
  };
}
const flush = () => Promise.resolve();

for (const observer of [true, false]) test(`narrow hidden figure waits, then loads once (observer: ${observer})`, async t => {
  const env = environment(t, { narrow: true, observer });
  env.setVisible(false); env.painted(); env.intersect(); env.fallback();
  assert.equal(env.imports, 0, 'neither a stale intersection nor the timer loads a hidden card');
  env.setVisible(true);
  assert.equal(env.imports, 0);
  env.intersect(); env.fallback(); await flush();
  assert.equal(env.calls, 1);
  env.intersect(); env.fallback();
  assert.equal(env.imports, 1);
});

test('narrow background tab waits through the fallback and loads when seen', async t => {
  const env = environment(t, { narrow: true });
  env.doc.hidden = true; env.painted(); env.intersect(); env.fallback();
  assert.equal(env.imports, 0);
  assert.equal(env.timers.size, 1);
  env.doc.hidden = false; env.doc.emit('visibilitychange'); await flush();
  assert.equal(env.calls, 1);
  assert.equal(env.timers.size, 0);
});

test('narrow figure outside the viewport is not loaded by the safety timer', async t => {
  const env = environment(t, { narrow: true });
  env.painted(); env.intersect(false); env.fallback();
  assert.equal(env.imports, 0);
  env.intersect(); await flush();
  assert.equal(env.calls, 1);
});

test('only imports once after a paint and intersection, cleaning every waiting resource', async t => {
  const env = environment(t);
  assert.equal(env.imports, 0);
  env.intersect(); env.tick();
  assert.equal(env.connected, false);
  assert.equal(env.imports, 0);
  env.tick();
  assert.equal(env.connected, true);
  env.intersect(false);
  assert.equal(env.imports, 0);
  env.intersect(); env.intersect(); env.fallback();
  await flush();
  assert.equal(env.imports, 1); assert.equal(env.calls, 1);
  assert.equal(env.connected, false);
  assert.equal(env.frames.size + env.timers.size, 0);
});

test('latest measurements, toggle, reduced motion and visibility are read after the pending import', async t => {
  let complete;
  const env = environment(t, { load: createBody => new Promise(resolve => { complete = () => resolve({ createBody }); }) });
  env.controller.setBodyMetrics({ weightKg: 70 });
  env.painted(); env.intersect();
  env.controller.setBodyMetrics({ weightKg: 82, heightCm: 180 });
  env.setMotion(false);
  env.doc.hidden = true;
  complete(); await flush();
  assert.deepEqual(env.applied.metrics, [{ weightKg: 82, heightCm: 180 }]);
  assert.deepEqual(env.applied.motion, [false, false]);
  env.setMotion(true);
  assert.equal(env.applied.motion.at(-1), false);
  env.doc.hidden = false; env.doc.emit('visibilitychange');
  assert.equal(env.applied.motion.at(-1), true);
  env.reduced.matches = true; env.reduced.emit('change');
  assert.equal(env.applied.motion.at(-1), false);
  env.reduced.matches = false; env.reduced.emit('change');
  env.setVisible(false);
  assert.equal(env.applied.motion.at(-1), false);
  env.setVisible(true);
  assert.equal(env.applied.motion.at(-1), true);
  env.controller.setBodyMetrics({ weightKg: 83 });
  assert.deepEqual(env.applied.metrics.at(-1), { weightKg: 83 });
});

test('no visibility observer still loads after painting', async t => {
  const env = environment(t, { observer: false });
  assert.equal(env.imports, 0);
  env.painted(); await flush();
  assert.equal(env.calls, 1);
});

test('hidden tab with suspended animation frames has a bounded fallback', async t => {
  const env = environment(t);
  env.doc.hidden = true;
  assert.deepEqual([...env.timers.values()].map(timer => timer.ms), [5000]);
  env.fallback(); await flush();
  assert.equal(env.imports, 1); assert.equal(env.calls, 1);
  assert.equal(env.applied.motion.at(-1), false);
  assert.equal(env.frames.size, 0);
  env.doc.hidden = false; env.doc.emit('visibilitychange');
  assert.equal(env.applied.motion.at(-1), true);
});

test('a model on another page is not fetched by the fallback; it loads once shown', async t => {
  const env = environment(t);
  env.setVisible(false); env.painted();
  env.fallback();
  assert.equal(env.imports, 0, 'the fallback alone does not download a hidden model');
  assert.deepEqual([...env.timers.values()].map(timer => timer.ms), [5000], 'the wait is re-armed');
  env.fallback(); await flush();
  assert.equal(env.imports, 0);
  env.setVisible(true); env.intersect(); await flush();
  assert.equal(env.calls, 1);
});

test('hidden model or silent observer cannot prevent loading indefinitely', async t => {
  const env = environment(t);
  env.setVisible(false); env.painted(); env.intersect(false);
  assert.equal(env.imports, 0);
  env.fallback(); env.intersect(); await flush();
  assert.equal(env.calls, 1);
  assert.equal(env.applied.motion.at(-1), false);
});

test('an intersection delivered in the background loads when the tab becomes visible', async t => {
  const env = environment(t);
  env.doc.hidden = true; env.painted(); env.intersect();
  assert.equal(env.imports, 0);
  env.doc.hidden = false; env.doc.emit('visibilitychange'); await flush();
  assert.equal(env.calls, 1);
  assert.equal(env.timers.size, 0);
});

test('import failure is contained and never retried by visibility or fallback', async t => {
  const env = environment(t, { load: () => Promise.reject(new Error('offline')) });
  env.painted(); env.intersect(); await flush();
  assert.equal(env.container.dataset.modelFailed, 'true');
  assert.equal(env.calls, 0);
  env.controller.setBodyMetrics({ weightKg: 88 }); env.setMotion(false);
  env.intersect(); env.fallback();
  assert.equal(env.imports, 1);
});

test('pagehide cancels delayed import and removes state listeners', t => {
  const env = environment(t);
  env.painted(); env.win.emit('pagehide'); env.intersect(); env.fallback();
  assert.equal(env.imports, 0);
  assert.equal(env.connected, false);
  assert.equal(env.timers.size + env.frames.size, 0);
  assert.equal(env.doc.listeners.size + env.reduced.listeners.size, 0);
});

test('a page restored from the back/forward cache shows the silhouette, then a fresh model', async t => {
  const env = environment(t);
  env.controller.setBodyMetrics({ weightKg: 74 });
  env.painted(); env.intersect(); await flush();
  assert.equal(env.calls, 1);
  env.container.dataset.modelLoaded = 'true';
  env.win.emit('pagehide');
  assert.equal(env.applied.disposed, 1);
  assert.equal('modelLoaded' in env.container.dataset, false, 'the silhouette is uncovered');
  env.win.listeners.get('pageshow')({ persisted: false });
  env.painted(); env.intersect(); env.fallback(); await flush();
  assert.equal(env.calls, 1, 'a fresh page load is not a restore');
  env.win.listeners.get('pageshow')({ persisted: true });
  assert.equal(env.doc.listeners.has('visibilitychange'), true);
  assert.deepEqual([...env.timers.values()].map(timer => timer.ms), [5000]);
  env.painted(); env.intersect(); await flush();
  assert.equal(env.calls, 2);
  assert.deepEqual(env.applied.metrics.at(-1), { weightKg: 74 }, 'the latest measurements reach the new model');
  env.win.listeners.get('pageshow')({ persisted: true });
  assert.equal(env.calls, 2, 'a second pageshow without a pagehide changes nothing');
});

test('a restore after a failed import keeps the silhouette and does not retry', async t => {
  const env = environment(t, { load: () => Promise.reject(new Error('offline')) });
  env.painted(); env.intersect(); await flush();
  env.win.emit('pagehide');
  env.win.listeners.get('pageshow')({ persisted: true });
  env.painted(); env.intersect(); env.fallback(); await flush();
  assert.equal(env.imports, 1);
  assert.equal(env.container.dataset.modelFailed, 'true');
});
