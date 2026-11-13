import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createFramePainter, parseFrame, summarize } from '../../frontend/src/chat-stream.js';
import { limitLinks } from '../../frontend/src/chat-format.js';

// A fake animation-frame clock: frames run only when the test says so.
function frames() {
  const queue = new Map();
  let serial = 0, count = 0;
  return {
    request(callback) { queue.set(++serial, callback); return serial; },
    cancel(id) { queue.delete(id); },
    run() {
      if (!queue.size) return;
      count++;
      const pending = [...queue.values()];
      queue.clear();
      pending.forEach(callback => callback());
    },
    get count() { return count; },
    get pending() { return queue.size; },
  };
}

test('a 20 KB synthetic stream paints at most once per animation frame', () => {
  const clock = frames();
  const paints = [];
  const painter = createFramePainter((id, text) => paints.push([id, text]), clock);
  const answer = 'Uyku düzenin bu hafta istikrarlı. '.repeat(600).slice(0, 20 * 1024);
  let text = '';
  // Deltas of 1–40 characters; a frame passes after every few of them, as on a busy socket.
  for (let at = 0, step = 0; at < answer.length; step++) {
    const size = 1 + (step * 7) % 40;
    text += answer.slice(at, at + size);
    at += size;
    painter.queue('answer', text);
    if (step % 5 === 4) clock.run();
  }
  clock.run();
  assert.ok(paints.length > 1, 'the stream is painted progressively');
  assert.ok(paints.length <= clock.count, `${paints.length} paints in ${clock.count} frames`);
  assert.equal(paints.at(-1)[1], answer, 'the last paint carries the whole text');
  assert.equal(clock.pending, 0);
});

test('a final text drops the pending paint; clearing cancels the frame', () => {
  const clock = frames();
  const paints = [];
  const painter = createFramePainter((id, text) => paints.push([id, text]), clock);
  painter.queue('a', 'Mer');
  painter.queue('b', 'İkinci');
  painter.drop('a');
  clock.run();
  assert.deepEqual(paints, [['b', 'İkinci']]);
  painter.queue('a', 'Merhaba');
  painter.drop('a');
  assert.equal(clock.pending, 0, 'no frame is left waiting for nothing');
  painter.queue('a', 'Merhaba');
  painter.clear();
  clock.run();
  assert.deepEqual(paints, [['b', 'İkinci']]);
});

test('broken frames are skipped and logged by size only; valid frames pass through', () => {
  const logged = [];
  const warn = message => logged.push(message);
  for (const frame of ['{"type":"delta","text":"yarım', 'null', '42', '"metin"', '[]', '{"text":"türsüz"}', '', undefined])
    assert.equal(parseFrame(frame, warn), null, String(frame));
  assert.equal(logged.length, 8);
  assert.ok(logged.every(message => !message.includes('yarım') && !message.includes('türsüz')), 'frame text is never logged');
  assert.deepEqual(parseFrame('{"type":"delta","id":"x","text":"Merhaba"}', warn), {type: 'delta', id: 'x', text: 'Merhaba'});
  assert.equal(logged.length, 8);
});

test('the screen reader summary is one short line', () => {
  assert.equal(summarize('  Merhaba.\n\n| a | b |\n  '), 'Merhaba. | a | b |');
  const long = summarize('Dinlenme nabzın bu hafta ortalama 58 atım. '.repeat(10));
  assert.ok(long.length <= 161 && long.endsWith('…'), long);
  assert.ok(!/\s…$/.test(long));
  assert.equal(summarize(null), '');
});

class Node {
  constructor(tagName, attributes = {}) { this.tagName = tagName; this.attributes = {...attributes}; }
  hasAttribute(name) { return name in this.attributes; }
  setAttribute(name, value) { this.attributes[name] = value; }
  getAttribute(name) { return this.attributes[name] ?? null; }
  removeAttribute(name) { delete this.attributes[name]; }
}

const SITE = 'https://healthos.example';
test('only links that stay on this site keep a target; they open in a new tab', () => {
  const local = new Node('A', {href: '/#/system'});
  limitLinks(local, SITE);
  assert.deepEqual(local.attributes, {href: '/#/system', target: '_blank', rel: 'noopener noreferrer'});
  for (const href of ['https://attacker.example/c?hrv=41&sleep=5h12', '//attacker.example/c', 'mailto:a@attacker.example',
    'data:text/html,x', 'javascript:alert(1)', 'http://healthos.example/insecure', 'https://healthos.example.attacker.example/'])  {
    const link = new Node('A', {href, title: 'başlık'});
    limitLinks(link, SITE);
    assert.deepEqual(link.attributes, {title: 'başlık'}, `${href} must lose its target`);
  }
  const anchor = new Node('A'), paragraph = new Node('P', {title: 'x'});
  limitLinks(anchor, SITE);
  limitLinks(paragraph, SITE);
  assert.deepEqual(anchor.attributes, {}, 'an anchor without href is not a link');
  assert.deepEqual(paragraph.attributes, {title: 'x'});
  // Its registration on the sanitizer is checked on rendered answers in tests/browser/umay-stream.cjs.
});
