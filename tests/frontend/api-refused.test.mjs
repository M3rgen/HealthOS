import assert from 'node:assert/strict';
import { test } from 'node:test';
import { refusedText } from '../../frontend/src/api.js';
import { tr } from '../../frontend/src/i18n/tr.js';
import { en } from '../../frontend/src/i18n/en.js';
import { SERVER_CODES, SERVER_MESSAGES } from '../../frontend/src/i18n/server-messages.js';

test('known server sentences and codes are said in the interface’s own words', () => {
  assert.equal(refusedText(422, 'Girdi geçersiz. Lütfen değerleri kontrol edin.'), tr.common.api.known.invalidInput);
  assert.doesNotMatch(refusedText(422, 'Girdi geçersiz. Lütfen değerleri kontrol edin.'), /edin/, 'informal register');
  assert.equal(refusedText(422, 'Ölçüm 50–250 aralığında olmalı.'), 'Ölçüm 50–250 aralığında olmalı.');
  assert.equal(refusedText(422, { code: 'coverage_invalid_range', max_days: 31 }), 'Dönem en çok 31 gün olabilir.');
  assert.equal(refusedText(422, { code: 'something_new' }), tr.common.api.refused.invalid);
});

test('a sentence in the other language is never shown; the status picks the words', () => {
  assert.equal(refusedText(403, 'A same-origin job request is required'), tr.common.api.refused.forbidden);
  assert.equal(refusedText(429, 'Çok fazla istek. Lütfen bekleyin.', 'en'), tr.common.api.refused.rateLimited);
  assert.equal(refusedText(422, [{ msg: 'Input should be a valid date or datetime, input is too short' }]), tr.common.api.refused.invalid);
  assert.equal(refusedText(500, 'Internal Server Error'), tr.common.api.refused.server);
  assert.equal(refusedText(418, null), tr.common.api.failed);
  // Already in the interface's language: shown as the server wrote it.
  assert.equal(refusedText(500, 'Google bağlantısı kurulamadı.'), 'Google bağlantısı kurulamadı.');
  assert.equal(refusedText(503, 'Garmin status unavailable', 'en'), 'Garmin status unavailable');
});

test('every mapped key exists in both dictionaries', () => {
  for (const key of [...Object.values(SERVER_MESSAGES), ...Object.values(SERVER_CODES), 'measurementRange']) {
    assert.ok(tr.common.api.known[key], `tr ${key}`);
    assert.ok(en.common.api.known[key], `en ${key}`);
  }
});
