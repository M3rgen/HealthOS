import assert from 'node:assert/strict';
import { test } from 'node:test';
import { garminPull, sourceDetails } from '../../frontend/src/sources.js';
import { state } from '../../frontend/src/state.js';

state.zone = 'UTC';
const garmin = reasons => ({ active_sources: ['garmin'], reasons, garmin: { last_success_at: '2026-10-16T14:00:00Z' } });
const detail = health => sourceDetails(health).find(([label]) => label === 'Garmin')?.[1];

test('/health reasons name the Garmin pull state; a failure outranks a late pull', () => {
  assert.equal(garminPull(garmin([])).key, 'ok');
  assert.equal(garminPull(garmin(['garmin_pull_stale'])).key, 'stale');
  assert.equal(garminPull(garmin(['garmin_pull_stale', 'garmin_pull_failed'])).key, 'failed');
  // Not every kind has run yet, but one has: data is coming in, nothing to warn about.
  assert.equal(garminPull(garmin(['garmin_pull_uninitialized'])).key, 'ok');
  assert.equal(garminPull({ active_sources: ['garmin'], reasons: ['garmin_pull_uninitialized'], garmin: { last_success_at: null } }).key, 'waiting');
  assert.equal(garminPull({ active_sources: ['garmin'] }).key, 'ok', 'an older report without the field');
});

test('the Sistem row says what failed and when the last good pull was', () => {
  assert.match(detail(garmin([])), /^Seçili · son başarılı çekim \d+ Eki 14:00$/);
  assert.match(detail(garmin(['garmin_pull_failed'])), /^Son çekim başarısız · son başarılı çekim/);
  assert.match(detail(garmin(['garmin_pull_stale'])), /^Çekim gecikiyor · /);
  assert.equal(detail({ active_sources: ['garmin'], reasons: ['garmin_pull_uninitialized'] }), 'İlk çekim bekleniyor');
  assert.equal(detail({ active_sources: ['google'], reasons: ['garmin_pull_failed'] }), 'Bağlı değil');
});
