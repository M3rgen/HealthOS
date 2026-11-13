import assert from 'node:assert/strict';
import { test } from 'node:test';
import { NOTICE_TIMEOUT_MS, createNotice } from '../../frontend/src/notice.js';

class Element {
  hidden = true; textContent = ''; title = ''; dataset = {}; attributes = {}; handlers = {}; classes = new Set();
  classList = { remove: name => this.classes.delete(name), toggle: (name, on) => (on ? this.classes.add(name) : this.classes.delete(name)) };
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(type, handler) { this.handlers[type] = handler; }
  before() {}
  contains(element) { return element === this || element === this.child; }
}
function setup() {
  const box = new Element(), text = new Element(), close = new Element(), action = new Element();
  box.child = close;
  const timers = new Map();
  let serial = 0, closed = 0;
  globalThis.document = { activeElement: null };
  const notice = createNotice({
    box, text, close, action, onClosed: () => closed++,
    timers: {
      setTimeout(callback, ms) { timers.set(++serial, { callback, ms }); return serial; },
      clearTimeout(id) { timers.delete(id); },
    },
  });
  const elapse = () => { const due = [...timers.values()]; timers.clear(); due.forEach(({ callback }) => callback()); };
  return { box, text, close, action, timers, notice, elapse, get closed() { return closed; } };
}

test('the close button has a name and hides any notice', () => {
  const env = setup();
  assert.equal(env.close.attributes['aria-label'], 'Bildirimi kapat');
  env.notice.show('Veriler alınamadı.', { error: true, source: 'dashboard', detail: 'HTTP 503' });
  assert.equal(env.box.hidden, false);
  assert.equal(env.text.title, 'HTTP 503');
  assert.ok(env.box.classes.has('error'));
  document.activeElement = env.close;
  env.close.handlers.click();
  assert.equal(env.box.hidden, true);
  assert.equal(env.text.textContent, '');
  assert.equal(env.box.classes.has('error'), false);
  assert.equal(env.closed, 1, 'focus leaves the removed button');
});

test('a success notice leaves on its own; an error waits for its own source', () => {
  const env = setup();
  env.notice.show('Ölçüm kaydedildi.');
  assert.deepEqual([...env.timers.values()].map(timer => timer.ms), [NOTICE_TIMEOUT_MS]);
  assert.equal(NOTICE_TIMEOUT_MS, 5000);
  env.elapse();
  assert.equal(env.box.hidden, true);

  env.notice.show('Veriler alınamadı.', { error: true, source: 'dashboard' });
  assert.equal(env.timers.size, 0, 'errors do not time out');
  env.notice.clear('measurements');
  assert.equal(env.box.hidden, false, 'another source leaves the error in place');
  env.notice.clear('dashboard');
  assert.equal(env.box.hidden, true);
  assert.equal(env.notice.source, null);
});

test('a newer notice is not hidden by the timer of the one it replaced', () => {
  const env = setup();
  env.notice.show('Eşitleme tamamlandı.', { source: 'sync-result' });
  env.notice.show('Eşitleme hatası: adım', { error: true, source: 'sync' });
  env.elapse();
  assert.equal(env.box.hidden, false);
  assert.equal(env.text.textContent, 'Eşitleme hatası: adım');
});

test('an action shows a button that runs it; a notice without one hides it', () => {
  const env = setup();
  let runs = 0;
  env.notice.show('Veriler alınamadı.', { error: true, source: 'dashboard', action: { label: 'Yeniden dene', run: () => runs++ } });
  assert.equal(env.action.hidden, false);
  assert.equal(env.action.textContent, 'Yeniden dene');
  env.action.handlers.click();
  assert.equal(runs, 1);
  env.notice.show('Ölçüm kaydedildi.');
  assert.equal(env.action.hidden, true);
  assert.equal(env.action.textContent, '');
  env.action.handlers.click();
  assert.equal(runs, 1, 'a stale action no longer runs');
});

test('removing the notice while focus is inside hands focus back', () => {
  const env = setup();
  env.notice.show('Veriler alınamadı.', { error: true, source: 'dashboard', action: { label: 'Yeniden dene', run() {} } });
  document.activeElement = env.close;
  env.notice.clear('dashboard');
  assert.equal(env.box.hidden, true);
  assert.equal(env.closed, 1);
});
