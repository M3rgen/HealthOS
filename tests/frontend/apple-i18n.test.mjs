// The Apple Health panel's own dictionary (GitHub-only Apple extension): English mirrors Turkish.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { en, tr } from '../../frontend/src/i18n/apple.js';

const kind = value => (typeof value === 'function' ? `function/${value.length}` : typeof value);

test('the Apple panel text has the same keys, types and arguments in both languages', () => {
  assert.deepEqual(Object.keys(en).sort(), Object.keys(tr).sort());
  for (const key of Object.keys(tr)) assert.equal(kind(en[key]), kind(tr[key]), key);
});

test('the English Apple panel text has no Turkish letters', () => {
  const sample = Object.values(en).map(value => (typeof value === 'function' ? value('1', '2', '3') : value));
  assert.deepEqual(sample.filter(text => /[çğıöşüÇĞİÖŞÜ]/.test(text)), []);
});
