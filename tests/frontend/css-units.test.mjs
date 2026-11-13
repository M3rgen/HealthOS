import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { test } from 'node:test';

// Text sizes are set in rem so the reader's browser font-size preference applies. Layout lengths
// (padding, gaps, panels) may stay in px; type may not.
const SRC = new URL('../../frontend/src/', import.meta.url);
const sheets = [
  ...readdirSync(new URL('styles/', SRC)).filter(name => name.endsWith('.css')).map(name => `styles/${name}`),
  ...readdirSync(SRC).filter(name => name.endsWith('.css')),
];
const TYPE_PROPERTIES = /(?:^|[;{\s])(font-size|line-height|letter-spacing)\s*:\s*([^;}]*)/g;

test('text sizes never use px', () => {
  for (const sheet of sheets) {
    const css = readFileSync(new URL(sheet, SRC), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
    for (const [, property, value] of css.matchAll(TYPE_PROPERTIES))
      assert.ok(!/\dpx/.test(value), `${sheet}: ${property}: ${value.trim()} uses px; use rem`);
  }
});
