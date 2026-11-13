import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const { VIEWPORTS, LANGUAGES, args } = require('../../frontend/preview/shoot.cjs');
const { sizes } = require('../browser/viewport-floors.json');

test('screenshot defaults cover the pinned matrix in both languages with still motion', () => {
  assert.deepEqual(VIEWPORTS, sizes.map(([w, h]) => `${w}x${h}`));
  assert.deepEqual(LANGUAGES, ['tr', 'en']);
  assert.equal(args([]).reducedMotion, true);
  assert.match(args([]).out, /\.test-output\/shoot$/);
  assert.deepEqual(args(['--viewports=320x256', '--views=system']).viewports, ['320x256']);
  assert.throws(() => args(['--views=missing']), /Unknown views/);
});
