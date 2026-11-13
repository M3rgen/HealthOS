import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

const frontend = fileURLToPath(new URL('../../frontend/', import.meta.url));
const { ESLint } = createRequire(new URL('../../frontend/package.json', import.meta.url))('eslint');

// Static check: every value placed into HTML built with a template string passes through
// escape() or num(), or is named in the explicit allow-list below with the reason it is safe.
// A template is HTML when its own text contains a tag; templates nested inside an HTML template's
// ${…} are checked too, because their result lands in the same markup.
const ALLOW = {
  'body.js': {},
  'connection-panel.js': {},
  'coverage.js': {
    sources: 'list items built from escape()/num() values just above',
    exclusions: 'joined escape()/num() values just above',
  },
  'devices.js': {
    'when(device.last_data, text.noDatedRecordInline)': 'when() returns escaped <time> markup',
    'when(device.google_last_sync)': 'when() returns escaped <time> markup',
    'when(device.garmin_last_pull)': 'when() returns escaped <time> markup',
    google: 'built above from when()',
    pull: 'built above from when()',
    rank: 'built above with escape()',
    value: 'detail() callers pass escaped text or markup built with escape()',
  },
  'history-table.js': {
    others: 'alternativesText() escapes its labels and numbers',
    "sourceBadge(row, 'steps')": 'sourceBadge() escapes the source label',
    "sourceBadge(row, 'sleep_seconds')": 'sourceBadge() escapes the source label',
    "sourceBadge(row, 'resting_bpm')": 'sourceBadge() escapes the source label',
    "sourceBadge(row, 'hrv_ms')": 'sourceBadge() escapes the source label',
    "sourceBadge(row, 'spo2')": 'sourceBadge() escapes the source label',
    'duration(row.sleep_seconds)': 'duration() builds text from a number',
    toggle: 'built above with escape()',
    subRow: 'built above with escape()',
    subId: 'yyyy-mm-dd key of the API row, and its id attribute only',
  },
};
const FILES = Object.keys(ALLOW);

/**
 * Template literals of a JavaScript source, each with its static text and its ${…} expressions.
 * An expression's `shape` replaces nested string literals with S and template literals with T.
 * Strings, comments and regular expression literals are skipped, so their contents never count.
 */
export function templates(source) {
  let at = 0;
  const top = [];
  const regexBefore = /[(,=:[!&|?{};+\-*%<>~^]$|^$|\breturn$|=>$/;
  function skipString(quote) {
    for (at++; at < source.length && source[at] !== quote; at++) if (source[at] === '\\') at++;
    at++;
  }
  function skipRegex() {
    let inClass = false;
    for (at++; at < source.length; at++) {
      const c = source[at];
      if (c === '\\') at++;
      else if (c === '[') inClass = true;
      else if (c === ']') inClass = false;
      else if (c === '/' && !inClass) break;
    }
    at++;
    while (/[a-z]/i.test(source[at] ?? '')) at++;
  }
  // Reads code until an unmatched `}` (inside ${…}) or the end; returns its shape and templates.
  function code(stopAtBrace) {
    let shape = '', depth = 0;
    const found = [];
    while (at < source.length) {
      const c = source[at], next = source[at + 1];
      if (c === '/' && next === '/') { at = source.indexOf('\n', at); if (at < 0) at = source.length; continue; }
      if (c === '/' && next === '*') { at = source.indexOf('*/', at) + 2; continue; }
      if (c === '"' || c === "'") { skipString(c); shape += 'S'; continue; }
      if (c === '`') { found.push(template()); shape += 'T'; continue; }
      if (c === '/' && regexBefore.test(shape.trimEnd())) { skipRegex(); shape += 'R'; continue; }
      if (c === '{') depth++;
      if (c === '}') {
        if (stopAtBrace && depth === 0) { at++; return { shape: shape.trim(), templates: found }; }
        depth--;
      }
      shape += c;
      at++;
    }
    return { shape: shape.trim(), templates: found };
  }
  function template() {
    const node = { text: '', expressions: [] };
    for (at++; at < source.length;) {
      const c = source[at];
      if (c === '\\') { node.text += source.slice(at, at + 2); at += 2; continue; }
      if (c === '`') { at++; break; }
      if (c === '$' && source[at + 1] === '{') {
        const start = at + 2;
        at = start;
        const { shape, templates: nested } = code(true);
        node.expressions.push({ source: source.slice(start, at - 1).trim(), shape, templates: nested });
        continue;
      }
      node.text += c;
      at++;
    }
    node.html = /<\/?[a-z]/i.test(node.text);
    return node;
  }
  top.push(...code(false).templates);
  return top;
}

// Splits `a ? b : c` at its top level; `?.` and `??` are not conditionals.
function conditional(shape) {
  let depth = 0, question = -1;
  for (let i = 0; i < shape.length; i++) {
    const c = shape[i];
    if ('([{'.includes(c)) depth++;
    else if (')]}'.includes(c)) depth--;
    else if (depth === 0 && c === '?' && shape[i + 1] !== '.' && shape[i + 1] !== '?' && shape[i - 1] !== '?') {
      if (question < 0) question = i;
      else depth++; // a nested conditional: its ':' belongs to it
    } else if (depth > 0 && c === ':' && question >= 0) depth--;
    else if (depth === 0 && c === ':' && question >= 0) return [shape.slice(question + 1, i), shape.slice(i + 1)];
  }
  return null;
}
// escape(...) or num(...) wrapping the whole expression.
function wrapped(shape) {
  const match = /^(escape|num)\(/.exec(shape);
  if (!match || !shape.endsWith(')')) return false;
  let depth = 0;
  for (let i = match[0].length - 1; i < shape.length; i++) {
    if (shape[i] === '(') depth++;
    if (shape[i] === ')' && --depth === 0) return i === shape.length - 1;
  }
  return false;
}
function safeShape(shape) {
  shape = shape.trim();
  if (shape === 'S' || shape === 'T' || wrapped(shape)) return true;
  if (/^[\w$.?[\]()]+\.map\(.*=>\s*T\s*\)\s*\.join\(S\)$/s.test(shape)) return true;
  const branches = conditional(shape);
  return Boolean(branches && branches.every(safeShape));
}

/** The ${…} values of the file's HTML templates that are neither escaped nor allowed. */
export function unescaped(source, allow = {}) {
  const problems = [];
  const check = node => {
    for (const expression of node.expressions) {
      const allowed = expression.source in allow;
      if (!allowed && !safeShape(expression.shape)) problems.push(expression.source);
      // Text inside escape()/num() is escaped as a whole; anything else nested is markup.
      if (!wrapped(expression.shape)) expression.templates.forEach(check);
    }
  };
  const visit = node => {
    if (node.html) check(node);
    else node.expressions.forEach(expression => expression.templates.forEach(visit));
  };
  templates(source).forEach(visit);
  return problems;
}

test('HTML templates in the listed modules escape every value', () => {
  for (const file of FILES) {
    const source = readFileSync(new URL(`../../frontend/src/${file}`, import.meta.url), 'utf8');
    assert.deepEqual(unescaped(source, ALLOW[file]), [], `${file}: unescaped template values`);
  }
});

test('every allow-list entry is still used, so the list cannot hide new values', () => {
  for (const file of FILES) {
    const source = readFileSync(new URL(`../../frontend/src/${file}`, import.meta.url), 'utf8');
    for (const entry of Object.keys(ALLOW[file])) {
      const others = Object.fromEntries(Object.entries(ALLOW[file]).filter(([key]) => key !== entry));
      assert.ok(unescaped(source, others).includes(entry), `${file}: allow-list entry "${entry}" matches no template value`);
    }
  }
});

test('the checker reports raw values, nested markup and look-alike wrappers', () => {
  const source = [
    'const a = `<li>${escape(name)} · ${count}</li>`;',
    'const b = `<p>${ok ? `<b>${raw}</b>` : \'\'}${num(n)}</p>`;',
    'const c = `<td>${escaped(x)}${escape(y) + z}</td>`;',
    'const d = `${plain} text, not markup`; el.textContent = `${d}`;',
    'const e = `<ul>${items.map(item => `<li>${escape(item)}</li>`).join(\'\')}</ul>`;',
    'const f = `<i>${escape(`${inside} is escaped as a whole`)}</i>`; // `${comment}`',
    'const g = /`[^`]*`/.test(s) ? `<s>${"}"}</s>` : `<s>${level ? \'a\' : cond ? \'b\' : `${deep}`}</s>`;',
  ].join('\n');
  assert.deepEqual(unescaped(source), ['count', 'raw', 'escaped(x)', 'escape(y) + z', 'deep']);
  assert.deepEqual(unescaped(source, { count: 'test' }), ['raw', 'escaped(x)', 'escape(y) + z', 'deep']);
});

test('HTML sinks still reject raw strings, arbitrary calls and unbranded wrappers', async () => {
  const eslint = new ESLint({ cwd: frontend });
  const unsafe = [
    'el.innerHTML = raw;',
    'el.innerHTML = raw.unwrap();',
    'morphChildren(el, raw);',
    'morphChildren(el, new SafeHtml(raw));',
    'morphChildren(el, rows.map(row => row.html).join(""));',
    'morphChildren(el, arbitraryFragment(raw));',
    'el.innerHTML = chart(raw);',
    'el.innerHTML = markup.days(rows, zone).html;',
    'function fill(html) { el.innerHTML = html; }',
  ];
  const safe = [
    'el.innerHTML = `<b>${escape(raw)}</b>`;',
    'el.innerHTML = writeHtml(raw);',
    'el.innerHTML = garminNoteHtml(raw);',
    'el.innerHTML = systemDetailsHtml(raw, service);',
    'el.innerHTML = deviceDetailsMarkup(raw, status, stamp);',
    'morphChildren(el, coverageMarkup(rows));',
    'morphChildren(el, devicesMarkup(rows, when));',
    'morphChildren(el, historyRowsMarkup(rows));',
    'el.innerHTML = measurementRowsMarkup(rows);',
    'el.innerHTML = measurementMessageMarkup(raw);',
    'el.innerHTML = metricHistoryChartMarkup(metric, days, width, height);',
    'morphChildren(el, syncRowsMarkup(rows));',
    'morphChildren(el, workoutDaysMarkup(rows, zone));',
    'morphChildren(el, webhookDetailsFragment(status, date));',
  ];
  for (const [snippets, expected] of [[unsafe, 1], [safe, 0]]) {
    for (const snippet of snippets) {
      const [result] = await eslint.lintText(snippet, { filePath: 'src/sink-probe.js' });
      const messages = result.messages.filter(message => message.ruleId?.startsWith('no-unsanitized/'));
      assert.equal(messages.length, expected, `${snippet}: ${JSON.stringify(messages)}`);
    }
  }
});
