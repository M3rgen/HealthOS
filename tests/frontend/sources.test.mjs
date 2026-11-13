import assert from 'node:assert/strict';
import { test } from 'node:test';
import { paintSources, sourceDetails, sourceEnabled } from '../../frontend/src/sources.js';

test('source UI uses the active list and blocks absent Google actions', () => {
  const nodes = new Map();
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, {});
    return nodes.get(id);
  };
  const root = { getElementById: node, querySelector: node };
  const health = { active_sources: ['garmin', 'apple'], configuration: 'healthy' };
  paintSources(health, root);
  assert.equal(node('sync-button').disabled, true);
  assert.equal(node('[href="/auth/google/start"]').hidden, true);
  assert.equal(node('google-empty').hidden, true);
  assert.equal(node('webhook-status-open').disabled, true);
  assert.equal(node('coverage-open').disabled, true);
  assert.equal(node('.coverage-panel').hidden, true, 'no Google panels without Google');
  assert.equal(node('.webhook-status-panel').hidden, true);
  assert.equal(node('coverage-summary').textContent, 'Google bağlı değil.');
  assert.deepEqual(sourceDetails(health), [
    ['Google yapılandırması', 'Bağlı değil'], ['Garmin', 'Seçili'], ['Apple Sağlık', 'Seçili'],
  ]);
  assert(sourceDetails({ active_sources: [] }).every(([, value]) => value === 'Bağlı değil'));
  paintSources({ configuration: 'degraded' }, root);
  assert.equal(node('sync-button').disabled, false);
  assert.equal(node('coverage-open').disabled, false);
  assert.equal(node('.coverage-panel').hidden, false);
  assert.equal(node('.webhook-status-panel').hidden, false);
  assert.equal(node('[href="/auth/google/start"]').hidden, false);
  assert.equal(node('google-empty').hidden, false);
  assert.equal(sourceEnabled({}, 'google'), true);
  assert.equal(sourceEnabled({}, 'garmin'), false);
});

test('a selected source without its importer reads "Kurulu değil", not "Seçili"', () => {
  const missing = { active_sources: ['apple'], configuration: 'healthy', reasons: ['apple_importer_not_installed'] };
  assert.deepEqual(sourceDetails(missing).at(-1), ['Apple Sağlık', 'Kurulu değil']);
  const installed = { active_sources: ['apple'], configuration: 'healthy', reasons: [] };
  assert.deepEqual(sourceDetails(installed).at(-1), ['Apple Sağlık', 'Seçili']);
  assert.deepEqual(sourceDetails({ active_sources: [], reasons: ['apple_importer_not_installed'] }).at(-1), ['Apple Sağlık', 'Bağlı değil']);
});

test('Garmin details append the key permission warning only when reported', () => {
  const health = { active_sources: ['garmin'], garmin: { last_success_at: '2026-11-10T12:00:00Z' }, reasons: [] };
  const detail = sourceDetails(health)[1][1];
  const warning = 'anahtar dosyasının izni gevşek (600 olmalı)';
  assert(!detail.includes(warning));
  assert.equal(sourceDetails({ ...health, reasons: ['garmin_key_file_permissive'] })[1][1], `${detail} · ${warning}`);
  assert.equal(sourceDetails({ ...health, active_sources: [], reasons: ['garmin_key_file_permissive'] })[1][1], 'Bağlı değil');
});
