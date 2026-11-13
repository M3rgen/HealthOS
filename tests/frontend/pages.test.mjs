import test from 'node:test';
import assert from 'node:assert/strict';
import { PAGES, pageFromHash } from '../../frontend/src/pages.js';

test('every page has a route and only #/<page> names one', () => {
  assert.deepEqual(PAGES, ['overview', 'measurements', 'system', 'sports']);
  for (const page of PAGES) assert.equal(pageFromHash(`#/${page}`), page);
  // A bare #sports would match <section id="sports"> and scroll the document to it.
  for (const hash of ['', '#', '#/', '#sports', '#/sport', '#/sports/', '#/SPORTS', '#//sports']) assert.equal(pageFromHash(hash), null, hash);
});
