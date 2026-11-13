import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { expandBreakpoints } from '../../frontend/css-breakpoints.js';
import { widths, heights, queries, JS_ONLY } from '../../frontend/src/breakpoints.js';
import { NARROW } from '../../frontend/src/overview-tabs.js';

const SRC = new URL('../../frontend/src/', import.meta.url);
const sourceFiles = extension => readdirSync(SRC, { recursive: true }).filter(file => file.endsWith(extension));
const conditions = query => [...query.matchAll(/\(\s*(?:min|max)-(?:width|height)\s*:[^)]+\)/g)]
  .map(([condition]) => condition.replace(/\s/g, ''));
const cssConditions = new Set(sourceFiles('.css').flatMap(file => {
  const css = readFileSync(new URL(file, SRC), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
  return [...css.matchAll(/@media[^\{]+/g)].flatMap(([query]) => conditions(expandBreakpoints(query)));
}));

test('shared JS and CSS queries expand to the existing em thresholds', () => {
  assert.deepEqual(queries, {
    umayModal: '(max-width: 43.75em), (max-height: 30em)',
    besideCards: '(max-height: 30em) and (min-width: 35.5em)',
    shortScreen: '(max-height: 30em)',
    overviewTabs: '(max-width: 56.25em), (max-height: 30em)',
    systemTabs: '(max-width: 56.25em), (max-height: 35em)',
  });
  assert.equal(NARROW, queries.overviewTabs);
  assert.equal(NARROW, '(max-width: 56.25em), (max-height: 30em)');
  assert.equal(expandBreakpoints('(min-width: bp(w901)) and (max-width: bp(w1400))'),
    '(min-width: 56.3125em) and (max-width: 87.5em)');
  assert.equal(expandBreakpoints('(prefers-reduced-motion: reduce)'), '(prefers-reduced-motion: reduce)');
  assert.throws(() => expandBreakpoints('(max-width: bp(typo))'), /Unknown breakpoint/);
});

test('every stylesheet media width uses the central breakpoint table', () => {
  const root = new URL('../../frontend/src/styles/', import.meta.url);
  const used = new Set();
  for (const file of readdirSync(root).filter(file => file.endsWith('.css'))) {
    const css = readFileSync(new URL(file, root), 'utf8');
    for (const [query] of css.matchAll(/@media[^\{]+/g)) {
      assert.doesNotMatch(query, /(?:min|max)-width:\s*[\d.]/, file);
      for (const [, token] of query.matchAll(/bp\((\w+)\)/g)) used.add(token);
      assert.doesNotMatch(expandBreakpoints(query), /bp\(/);
    }
  }
  for (const token of used) assert.ok(Object.hasOwn(widths, token));
  assert.ok(used.size > 10);
});

test('only breakpoints.js carries JS width or height query text, including nested sources', () => {
  const owners = sourceFiles('.js').filter(file =>
    /\(\s*(?:min|max)-(?:width|height)\s*:/.test(readFileSync(new URL(file, SRC), 'utf8')));
  assert.deepEqual(owners, ['breakpoints.js']);
});

test('every central query condition occurs in CSS media or the explicit JS-only list', () => {
  const tables = { widths, heights };
  const source = readFileSync(new URL('breakpoints.js', SRC), 'utf8')
    .replace(/\$\{(widths|heights)\.(\w+)\}/g, (_match, table, key) => {
      assert.ok(Object.hasOwn(tables[table], key), `Unknown ${table}.${key}`);
      return tables[table][key];
    });
  const exceptions = new Set(Object.values(JS_ONLY).flatMap(conditions));
  const all = conditions(source);
  assert.ok(all.length > 0);
  for (const condition of all) {
    assert.ok(cssConditions.has(condition) || exceptions.has(condition), `Missing CSS media counterpart: ${condition}`);
  }
});

test('JS-only exceptions cannot grow and must disappear when CSS gains their condition', () => {
  const approved = {
    umayModal: '(max-width: 43.75em)',
  };
  for (const [name, query] of Object.entries(JS_ONLY)) {
    assert.equal(query, approved[name], `Unapproved JS-only condition: ${name}`);
    for (const condition of conditions(query)) {
      assert.equal(cssConditions.has(condition), false, `Remove ${condition} from JS_ONLY.${name}: CSS now covers it`);
      assert.ok(conditions(queries[name]).includes(condition), `Unused JS-only condition: ${name}`);
    }
  }
});
