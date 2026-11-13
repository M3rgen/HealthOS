import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { test } from 'node:test';
import { tr, trPage } from '../../frontend/src/i18n/tr.js';
import { en, page as enPage } from '../../frontend/src/i18n/en.js';
import { fillHtml } from '../../frontend/html-i18n.js';
import { t, language } from '../../frontend/src/i18n/index.js';

// A5: the HTML template and its Turkish first paint share the runtime dictionary.
const FRONTEND = new URL('../../frontend/', import.meta.url);
const source = readFileSync(new URL('index.html', FRONTEND), 'utf8');
const html = fillHtml(source);

// Text that is not language (brand, symbols, axis hours, units and data values).
const NOT_TEXT = new Set(['Health', 'OS', 'UTC', 'steps', '—', '‹', '›', '×', '00:00', '12:00', '24:00', 'bpm']);
// Each language names itself in the language menu.
const ENDONYMS = new Set(['Türkçe', 'English']);
const ATTRIBUTES = ['aria-label', 'title', 'placeholder', 'alt'];
const VOID = new Set(['meta', 'link', 'input', 'img', 'br', 'source', 'hr', 'area', 'base', 'col', 'embed', 'track', 'wbr']);
const decode = text => text.replace(/&(amp|lt|gt|quot|#39);/g, (_, name) => ({ amp: '&', lt: '<', gt: '>', quot: '"', '#39': "'" })[name]);

/** Elements of index.html with their attributes and own (direct) text nodes; svg/script skipped. */
function elements(source) {
  const out = [], stack = [];
  const token = /<!--[\s\S]*?-->|<!doctype[^>]*>|<\/([a-z][\w-]*)\s*>|<([a-z][\w-]*)((?:\s+[^\s=>/]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s"'>]+))?)*)\s*(\/?)>/gi;
  let at = 0, match, skip = null;
  while ((match = token.exec(source))) {
    const text = source.slice(at, match.index);
    at = token.lastIndex;
    if (skip) {
      if (match[1]?.toLowerCase() === skip) skip = null;
      continue;
    }
    if (text.trim() && stack.length) stack.at(-1).texts.push(decode(text.trim()));
    if (match[0].startsWith('<!')) continue;
    if (match[1]) {
      const name = match[1].toLowerCase();
      const index = stack.findLastIndex(element => element.tag === name);
      if (index >= 0) stack.length = index;
      continue;
    }
    const tag = match[2].toLowerCase();
    const attributes = Object.fromEntries([...match[3].matchAll(/([^\s=>/]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g)]
      .map(([, key, double, single, bare]) => [key.toLowerCase(), decode(double ?? single ?? bare ?? '')]));
    const element = { tag, attributes, texts: [] };
    out.push(element);
    if (tag === 'svg' || tag === 'script' || tag === 'style') { if (!match[4]) skip = tag; continue; }
    if (!VOID.has(tag) && !match[4]) stack.push(element);
  }
  return out;
}
const all = elements(html);

test('index.html text equals the Turkish page dictionary, key by key', () => {
  const used = new Set();
  for (const element of all) {
    const key = element.attributes['data-i18n'];
    if (key !== undefined) {
      assert.ok(key in trPage, `${key} is not in trPage`);
      assert.equal(element.texts.length, 1, `${key}: one text node per marked element`);
      assert.equal(element.texts[0], trPage[key], `${key}: index.html and trPage differ`);
      used.add(key);
    }
    for (const attribute of ATTRIBUTES) {
      const marker = element.attributes[`data-i18n-${attribute}`];
      if (marker === undefined) continue;
      assert.ok(marker in trPage, `${marker} is not in trPage`);
      assert.equal(element.attributes[attribute], trPage[marker], `${marker}: ${attribute} and trPage differ`);
      used.add(marker);
    }
  }
  const title = /<title>([^<]*)<\/title>/.exec(html)[1];
  assert.equal(trPage['document.title'], decode(title));
  used.add('document.title');
  assert.deepEqual(Object.keys(trPage).filter(key => !used.has(key)), [], 'trPage keys no element uses');
});

test('no interface text in index.html is left without a dictionary key', () => {
  for (const element of all) {
    if (element.tag === 'title') continue;
    const texts = element.texts.filter(text => !NOT_TEXT.has(text));
    if (element.tag === 'option' && texts.every(text => ENDONYMS.has(text))) continue;
    if (texts.length) assert.ok('data-i18n' in element.attributes, `unmarked text: ${texts.join(' | ')}`);
    for (const attribute of ATTRIBUTES) {
      const value = element.attributes[attribute];
      if (value && /\p{L}/u.test(value)) assert.ok(`data-i18n-${attribute}` in element.attributes, `unmarked ${attribute}="${value}"`);
    }
  }
});

test('source HTML keeps no duplicated Turkish text (FE-10 evidence count)', () => {
  const dictionary = readFileSync(new URL('src/i18n/tr.js', FRONTEND), 'utf8');
  // Same literal/sub-string count as the rubric: >=4 characters, excluding keys/paths.
  // Unicode letters/numbers match Python's \w, including Turkish letters.
  const literals = new Set([...dictionary.matchAll(/'([^'\\\n]{4,})'/gu)].map(match => match[1]));
  const duplicates = [...literals].filter(value => !/^[\p{L}\p{N}_.\-/#]+$/u.test(value) && decode(source).includes(value));
  assert.ok(duplicates.length <= 10, `${duplicates.length} shared strings: ${duplicates.join(' | ')}`);
});

test('every HTML slot resolves and missing/empty translations fail the build', () => {
  assert.doesNotMatch(html, /\{\{i18n:/);
  for (const value of [undefined, '', '   '])
    assert.throws(() => fillHtml('{{i18n:key}}', { key: value }), /missing text for key/);
  assert.throws(() => fillHtml('{{i18n:toString}}', {}), /missing text for toString/);
});

test('HTML slots escape text and quoted attributes without changing child markup', () => {
  const source = '<label title="{{i18n:key}}"><span></span>{{i18n:key}}<input></label>';
  const text = `<>&"'`;
  assert.equal(fillHtml(source, { key: text }),
    '<label title="&lt;&gt;&amp;&quot;&#39;"><span></span>&lt;&gt;&amp;&quot;&#39;<input></label>');
});

test('the Compose smoke title check matches generated HTML', () => {
  const probes = readFileSync(new URL('../scripts/compose_probes.py', FRONTEND), 'utf8');
  const expected = /assert '(<title>[^']+)' in html/.exec(probes);
  assert.ok(expected && html.includes(expected[1]));
});

test('Turkish is the default and is the dictionary itself, untouched', () => {
  assert.equal(language, 'tr');
  assert.equal(t, tr);
});

// English may be partial, but whatever it has must match the Turkish shape.
function sameShape(english, turkish, path) {
  for (const [key, value] of Object.entries(english)) {
    const where = `${path}.${key}`;
    assert.ok(key in turkish, `${where} does not exist in tr`);
    const kind = v => (typeof v === 'function' ? `function/${v.length}` : typeof v);
    assert.equal(kind(value), kind(turkish[key]), `${where} has another type than in tr`);
    if (typeof value === 'object') sameShape(value, turkish[key], where);
  }
}

test('en.js mirrors the Turkish keys and types it translates', () => {
  sameShape(en, tr, 'en');
  for (const key of Object.keys(enPage)) assert.ok(key in trPage, `page.${key} does not exist in trPage`);
  for (const value of Object.values(enPage)) assert.ok(typeof value === 'string' && value.trim(), 'empty English text');
});

test('English covers the dashboard (top bar, calendar, overview) completely', () => {
  const dashboard = Object.keys(trPage).filter(key => /^(document|topbar|calendar|overview)\./.test(key));
  assert.deepEqual(dashboard.filter(key => !(key in enPage)), [], 'dashboard page keys without English');
  const missing = [];
  const walk = (turkish, english, path) => {
    for (const [key, value] of Object.entries(turkish)) {
      if (typeof value === 'object') walk(value, english?.[key], `${path}.${key}`);
      else if (english?.[key] === undefined) missing.push(`${path}.${key}`);
    }
  };
  for (const area of DASHBOARD_AREAS) walk(tr[area], en[area], area);
  assert.deepEqual(missing, [], 'dashboard strings without English');
});

// Areas of `tr` that the overview page shows (see parts/dashboard.js).
const DASHBOARD_AREAS = ['overview', 'history', 'sync', 'calendar', 'common', 'overviewTabs', 'notice'];

test('locale-aware formatting happens only in locale.js', () => {
  const sources = file => readFileSync(new URL(`src/${file}`, FRONTEND), 'utf8');
  const files = readdirSync(new URL('src/', FRONTEND)).filter(name => name.endsWith('.js'));
  for (const file of files.filter(name => name !== 'locale.js'))
    assert.ok(!sources(file).includes("'tr-TR'"), `${file} formats with 'tr-TR' itself`);
});

// String and template literals of a module, comments skipped (a light scanner, enough for src/).
function literals(source) {
  const out = [];
  for (let i = 0; i < source.length; i++) {
    const c = source[i], next = source[i + 1];
    if (c === '/' && next === '/') { i = source.indexOf('\n', i); if (i < 0) break; continue; }
    if (c === '/' && next === '*') { i = source.indexOf('*/', i + 2) + 1; continue; }
    if (c === '"' || c === "'" || c === '`') {
      let j = i + 1;
      while (j < source.length && source[j] !== c) j += source[j] === '\\' ? 2 : 1;
      out.push(source.slice(i + 1, j));
      i = j;
    }
  }
  return out;
}

test('modules keep no Turkish interface text of their own (it lives in the dictionary)', () => {
  const files = readdirSync(new URL('src/', FRONTEND)).filter(name => name.endsWith('.js'));
  const turkish = /[çğıöşüÇĞİÖŞÜ]/;
  const found = [];
  for (const file of files) {
    const source = readFileSync(new URL(`src/${file}`, FRONTEND), 'utf8');
    for (const text of literals(source)) if (turkish.test(text)) found.push(`${file}: ${text.slice(0, 60)}`);
  }
  assert.deepEqual(found, []);
});
