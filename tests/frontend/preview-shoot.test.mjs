import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const { VIEWPORTS, LANGUAGES, README, args } = require('../../frontend/preview/shoot.cjs');
const { sizes } = require('../browser/viewport-floors.json');

test('screenshot defaults cover the pinned matrix in both languages with still motion', () => {
  assert.deepEqual(VIEWPORTS, sizes.map(([w, h]) => `${w}x${h}`));
  assert.deepEqual(LANGUAGES, ['tr', 'en']);
  assert.equal(args([]).reducedMotion, true);
  assert.match(args([]).out, /\.test-output\/shoot$/);
  assert.deepEqual(args(['--viewports=320x256', '--views=system']).viewports, ['320x256']);
  assert.throws(() => args(['--views=missing']), /Unknown views/);
});

test('the README preset shoots English at 2x and keeps the matrix defaults untouched', () => {
  assert.deepEqual([args([]).languages, args([]).scale], [LANGUAGES, 1]);
  const readme = args(['--readme']);
  assert.deepEqual([readme.languages, readme.scale, readme.viewports], [['en'], 2, ['1440x900', '390x844']]);
  assert.deepEqual(readme.views, README.views);
  assert.deepEqual(args(['--readme', '--views', 'umay', '--scale=1']).views, ['umay']);
  assert.equal(args(['--readme', '--scale=1']).scale, 1);
  assert.deepEqual(args(['--languages', 'tr']).languages, ['tr']);
  assert.throws(() => args(['--languages=de']), /Unknown languages/);
  assert.throws(() => args(['--scale=0']), /--scale/);
});
