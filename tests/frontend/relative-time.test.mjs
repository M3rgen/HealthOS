import test from 'node:test';
import assert from 'node:assert/strict';
import { relativeTime } from '../../frontend/src/relative-time.js';

const now = new Date('2026-11-07T12:00:00Z');
const ago = (seconds) => new Date(now.getTime() - seconds * 1000).toISOString();

test('coarse Turkish distances, largest fitting unit', () => {
  assert.equal(relativeTime(ago(5), now), 'az önce');
  assert.equal(relativeTime(ago(59), now), 'az önce');
  assert.equal(relativeTime(ago(60), now), '1 dk önce');
  assert.equal(relativeTime(ago(45 * 60), now), '45 dk önce');
  assert.equal(relativeTime(ago(2 * 3600 + 7 * 60), now), '2 saat önce');
  assert.equal(relativeTime(ago(23 * 3600 + 3599), now), '23 saat önce');
  assert.equal(relativeTime(ago(24 * 3600), now), '1 gün önce');
  assert.equal(relativeTime(ago(6 * 86400 + 5), now), '6 gün önce');
  assert.equal(relativeTime(ago(7 * 86400), now), '1 hafta önce');
  assert.equal(relativeTime(ago(40 * 86400), now), '1 ay önce');
  assert.equal(relativeTime(ago(400 * 86400), now), '1 yıl önce');
  assert.equal(relativeTime(ago(800 * 86400), now), '2 yıl önce');
});

test('missing, unparsable and future values', () => {
  assert.equal(relativeTime(null, now), null);
  assert.equal(relativeTime('', now), null);
  assert.equal(relativeTime('not a date', now), null);
  // A client clock slightly ahead of the server never shows a negative age.
  assert.equal(relativeTime(new Date(now.getTime() + 30_000).toISOString(), now), 'az önce');
});
