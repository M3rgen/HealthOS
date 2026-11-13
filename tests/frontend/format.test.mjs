import assert from 'node:assert/strict';
import { test } from 'node:test';
import { duration, escape, num } from '../../frontend/src/format.js';

test('escape covers text and attribute contexts', () => {
  assert.equal(escape(`<a href="x" title='y'>&</a>`), '&lt;a href=&quot;x&quot; title=&#39;y&#39;&gt;&amp;&lt;/a&gt;');
  assert.equal(escape(null), '');
  assert.equal(escape(0), '0');
});

test('num uses Turkish grouping, never markup, and a dash when missing', () => {
  assert.equal(num(12345), '12.345');
  assert.equal(num(58.25, 1), '58,3');
  assert.equal(num(null), '—');
  assert.equal(num('<b>'), 'NaN');
});

test('duration drops the empty hour part', () => {
  assert.equal(duration(45 * 60), '45 dk');
  assert.equal(duration(7 * 3600 + 26 * 60), '7 sa 26 dk');
  assert.equal(duration(null), '—');
});
