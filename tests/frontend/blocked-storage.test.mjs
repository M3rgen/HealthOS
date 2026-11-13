import test from 'node:test';
import assert from 'node:assert/strict';
import { migrateLegacyKeys } from '../../frontend/src/legacy-names.js';
import { tr } from '../../frontend/src/i18n/tr.js';

test('a SecurityError from the localStorage getter does not break startup', async t => {
  const original = Object.getOwnPropertyDescriptor(globalThis, 'localStorage');
  Object.defineProperty(globalThis, 'localStorage', {
    get() { throw new DOMException('denied', 'SecurityError'); }, configurable: true,
  });
  try {
    await t.test('language module loads with Turkish fallback', async () => {
      const { language, t: strings } = await import('../../frontend/src/i18n/index.js?blocked');
      assert.equal(language, 'tr');
      assert.equal(strings, tr);
    });
    await t.test('legacy migration without an argument is safe', () => {
      assert.doesNotThrow(() => migrateLegacyKeys());
    });
  } finally {
    if (original) Object.defineProperty(globalThis, 'localStorage', original);
    else delete globalThis.localStorage;
  }
});
