import assert from 'node:assert/strict';
import { test } from 'node:test';
import { t } from '../../frontend/src/i18n/index.js';
import { en } from '../../frontend/src/i18n/en.js';
import { describeSyncError } from '../../frontend/src/sync-errors.js';

for (const [code, expected] of [
  [null, 'Tamamlanamadı.'],
  ['', 'Tamamlanamadı.'],
  ['rate_limited', 'Tamamlanamadı (rate_limited).'],
  ['X (rate_limited)', 'Tamamlanamadı (X (rate_limited)).'],
  ['process restarted', 'Yarıda kaldı; uygulama yeniden başlatıldı.'],
]) {
  test(`sync notice describes ${JSON.stringify(code)} without repeating its context`, () => {
    const description = describeSyncError(code);
    assert.equal(description, expected);
    const notice = t.sync.errors(`Adım: ${description}`);
    assert.equal(notice, `Eşitleme hatası: Adım: ${expected}`);
    assert.equal(notice.match(/Eşitleme/g)?.length, 1);
  });
}

test('English sync descriptions omit the outer sync context', () => {
  assert.equal(en.common.syncErrors.fallback, 'Could not be completed.');
  assert.equal(en.common.syncErrors.unknown('rate_limited'), 'Could not be completed (rate_limited).');
  assert.equal(en.common.syncErrors.restarted, 'Interrupted; the app restarted.');
});
