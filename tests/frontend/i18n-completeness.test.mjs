import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { tr, trPage } from '../../frontend/src/i18n/tr.js';
import { en, page as enPage } from '../../frontend/src/i18n/en.js';
import { merge } from '../../frontend/src/i18n/index.js';

const kind = value => (typeof value === 'function' ? `function/${value.length}` : Array.isArray(value) ? 'array' : typeof value);

function gaps(from, to, path = '') {
  const found = [];
  for (const [key, value] of Object.entries(from)) {
    const where = path ? `${path}.${key}` : key;
    if (!Object.hasOwn(to ?? {}, key)) found.push(where);
    else if (kind(value) !== kind(to[key])) found.push(`${where} (${kind(value)} vs ${kind(to[key])})`);
    else if (typeof value === 'object') found.push(...gaps(value, to[key], where));
  }
  return found;
}

test('en.js has every key of tr.js, with the same type and arguments', () => {
  assert.deepEqual(gaps(tr, en), []);
});

test('tr.js has every key of en.js', () => {
  assert.deepEqual(gaps(en, tr), []);
});

test('the English page dictionary matches the Turkish one in both directions', () => {
  assert.deepEqual(Object.keys(trPage).filter(key => !(key in enPage)), [], 'page keys without English');
  assert.deepEqual(Object.keys(enPage).filter(key => !(key in trPage)), [], 'English page keys without Turkish');
  for (const [key, value] of Object.entries(enPage)) assert.ok(typeof value === 'string' && value.trim(), `empty English text: ${key}`);
});

const strings = (dictionary, out = []) => {
  for (const value of Object.values(dictionary)) {
    if (typeof value === 'function') out.push(String(value));
    else if (value && typeof value === 'object') strings(value, out);
    else out.push(String(value));
  }
  return out;
};

test('no dictionary shows the word Codex to users', () => {
  for (const [name, dictionary] of Object.entries({ tr, trPage, en, enPage }))
    assert.deepEqual(strings(dictionary).filter(text => /codex/i.test(text)), [], name);
});

test('Turkish text uses "elle", never "Manuel", and no longer says "tur" or "köprü" for an Umay answer', () => {
  const turkish = [...strings(tr), ...strings(trPage)];
  assert.deepEqual(turkish.filter(text => /\bManuel\b/.test(text)), []);
  assert.deepEqual(turkish.filter(text => /Tur özeti|köprü|\bturda\b|turun/i.test(text)), []);
});

test('every data type the API can report has a readable name in both languages', () => {
  const source = readFileSync(new URL('../../app/google_health/data_types.py', import.meta.url), 'utf8');
  const google = [...source.matchAll(/DataTypeSpec\("([a-z0-9-]+)"/g)].map(match => match[1]);
  const kinds = readFileSync(new URL('../../app/services/providers/garmin_kinds.py', import.meta.url), 'utf8');
  const garmin = [...kinds.matchAll(/^\s*Kind\(\s*"([a-z0-9-]+)"/gm)].map(match => match[1]);
  const codes = [...google, ...garmin];
  assert.ok(google.length >= 30 && garmin.length >= 5, `found only ${google.length} + ${garmin.length} data types`);
  for (const code of codes) {
    assert.ok(tr.common.dataTypes[code], `no Turkish name for ${code}`);
    assert.ok(en.common.dataTypes[code], `no English name for ${code}`);
  }
});

test('a missing key falls back to the base text and is reported, not hidden', () => {
  const missing = [];
  const out = merge({ a: { b: 'Merhaba', c: 'Selam' } }, { a: { b: 'Hello' } }, where => missing.push(where));
  assert.deepEqual(out, { a: { b: 'Hello', c: 'Selam' } });
  assert.deepEqual(missing, ['a.c']);
});
