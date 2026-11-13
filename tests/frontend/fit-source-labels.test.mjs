import assert from 'node:assert/strict';
import { test } from 'node:test';
import { tr } from '../../frontend/src/i18n/tr.js';
import { en } from '../../frontend/src/i18n/en.js';
import { sourceLabel } from '../../frontend/src/data-types.js';

test('other FIT manufacturers have their own source label in each language', () => {
  for (const [dictionary, fit, manual, demo] of [
    [tr, 'FIT dosyası', 'Elle girildi', 'Örnek veri'],
    [en, 'FIT file', 'Entered by hand', 'Sample data'],
  ]) {
    assert.equal(dictionary.devices.platforms.FIT, fit);
    assert.equal(dictionary.devices.platforms.GARMIN, 'Garmin');
    assert.equal(dictionary.devices.platforms.MANUAL, manual);
    assert.equal(dictionary.devices.platforms.DEMO, demo);
  }
  assert.equal(sourceLabel('FIT', 'Garmin'), 'FIT dosyası');
  assert.equal(sourceLabel('GARMIN'), 'Garmin');
  assert.equal(sourceLabel('UNKNOWN', 'Device'), 'Device');
});
