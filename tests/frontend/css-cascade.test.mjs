import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { test } from 'node:test';

// A selector is declared at most once per media context in a sheet: overrides of a component live
// in that component's own block, not in a later rule that fights the first one.
const SRC = new URL('../../frontend/src/styles/', import.meta.url);

function rules(css) {
  const clean = css.replace(/\/\*[\s\S]*?\*\//g, '');
  const found = [];
  let i = 0;
  function block(context) {
    while (i < clean.length) {
      let head = '';
      while (i < clean.length && clean[i] !== '{' && clean[i] !== '}') head += clean[i++];
      if (clean[i] === '}' || i >= clean.length) { i++; return; }
      i++; // {
      const prelude = head.replace(/\s+/g, ' ').trim();
      if (prelude.startsWith('@keyframes') || prelude.startsWith('@font-face')) {
        let depth = 1;
        while (depth && i < clean.length) { if (clean[i] === '{') depth++; else if (clean[i] === '}') depth--; i++; }
      } else if (prelude.startsWith('@')) {
        block(`${context}${prelude} `);
      } else {
        found.push({ context, selectors: prelude.split(',').map(selector => selector.trim()) });
        let body = '';
        while (i < clean.length && clean[i] !== '}') body += clean[i++];
        i++;
      }
    }
  }
  block('');
  return found;
}

test('no selector is declared twice in one media context of a stylesheet', () => {
  const problems = [];
  for (const name of readdirSync(SRC).filter(file => file.endsWith('.css'))) {
    const seen = new Map();
    for (const { context, selectors } of rules(readFileSync(new URL(name, SRC), 'utf8'))) {
      for (const selector of selectors) {
        const key = `${context}|${selector}`;
        if (seen.has(key)) problems.push(`${name}: ${selector}${context ? ` in ${context.trim()}` : ''}`);
        seen.set(key, true);
      }
    }
  }
  assert.deepEqual(problems, []);
});
