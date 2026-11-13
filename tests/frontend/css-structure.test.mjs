import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { test } from 'node:test';

// The stylesheets stay readable and their precedence comes from cascade layers declared in
// style.css (base → components → overrides), never from the order modules happen to import CSS.
const SRC = new URL('../../frontend/src/', import.meta.url);
const read = path => readFileSync(new URL(path, SRC), 'utf8');
const sheets = [
  ...readdirSync(new URL('styles/', SRC)).filter(name => name.endsWith('.css')).map(name => `styles/${name}`),
  ...readdirSync(SRC).filter(name => name.endsWith('.css')),
];
const LAYERS = ['base', 'components', 'overrides'];

test('style.css declares the layer order before any rule and imports every sheet into a layer', () => {
  const css = read('style.css').replace(/\/\*[\s\S]*?\*\//g, '');
  const statements = css.split(';').map(part => part.trim()).filter(Boolean);
  assert.equal(statements[0], '@charset "UTF-8"');
  assert.equal(statements[1], `@layer ${LAYERS.join(', ')}`);
  const imports = [...css.matchAll(/@import\s+"([^"]+)"(?:\s+layer\(([\w-]+)\))?/g)];
  const layered = imports.filter(([, path]) => path.startsWith('./'));
  // Only the font's @font-face rules come in unlayered (they cannot conflict with any rule).
  assert.deepEqual(imports.filter(([, path]) => !path.startsWith('./')).map(([, path]) => path), ['@fontsource-variable/inter/opsz.css']);
  for (const [, path, layer] of layered) assert.ok(LAYERS.includes(layer), `${path} is imported without a known layer`);
  const imported = new Set(layered.map(([, path]) => path.slice(2)));
  for (const sheet of sheets.filter(name => !['style.css', 'body.css'].includes(name)))
    assert.ok(imported.has(sheet), `${sheet} is not imported (with a layer) by style.css`);
  // Layers follow the declared order in the import list too, so reading top-down matches the cascade.
  const order = layered.map(([, , layer]) => LAYERS.indexOf(layer));
  assert.deepEqual(order, [...order].sort((a, b) => a - b));
});

test('body.css, loaded later with the 3D viewer, puts its rules into the components layer', () => {
  const css = read('body.css').replace(/\/\*[\s\S]*?\*\//g, '').trim();
  assert.match(css, /^@layer components \{[\s\S]*\}$/);
});

test('JavaScript modules do not import stylesheets themselves (import order is not a cascade tool)', () => {
  const allowed = { 'main.js': ['./style.css'], 'body.js': ['./body.css'] };
  for (const file of readdirSync(SRC).filter(name => name.endsWith('.js'))) {
    const css = [...read(file).matchAll(/^import\s+'([^']+\.css)';/gm)].map(match => match[1]);
    assert.deepEqual(css, allowed[file] ?? [], `${file} imports CSS`);
  }
});

test('stylesheets are formatted one declaration per line and are not hand-minified', () => {
  for (const sheet of sheets) {
    const lines = read(sheet).replace(/\/\*[\s\S]*?\*\//g, match => match.replace(/[^\n]/g, '')).split('\n');
    lines.forEach((line, index) => {
      const where = `${sheet}:${index + 1}`;
      if (/url\("data:/.test(line)) return; // inline SVG data URIs stay on one line
      assert.ok(line.length <= 100, `${where} is ${line.length} characters long`);
      assert.ok(!/;\s*\S/.test(line.replace(/url\([^)]*\)/g, '')), `${where} holds more than one declaration`);
      assert.ok(!/\{\s*\S/.test(line), `${where} opens a rule and declares on the same line`);
    });
  }
});
