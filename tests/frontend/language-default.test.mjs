import assert from 'node:assert/strict';
import { test } from 'node:test';
import { browserLanguage, language } from '../../frontend/src/i18n/index.js';

// Without a stored choice the interface opens in the browser's language (i18n/index.js).
test('the browser language picks the interface language by primary subtag', () => {
  assert.equal(browserLanguage({ language: 'en-US', languages: ['en-US', 'en'] }), 'en');
  assert.equal(browserLanguage({ language: 'tr-TR', languages: ['tr-TR', 'tr'] }), 'tr');
  assert.equal(browserLanguage({ language: 'en-GB' }), 'en');
  // The first language the interface has wins, in the reader's order.
  assert.equal(browserLanguage({ languages: ['de-DE', 'tr', 'en'] }), 'tr');
  // Neither offered: English, which a stranger is likelier to read than Turkish.
  assert.equal(browserLanguage({ languages: ['de-DE', 'fr'] }), 'en');
  assert.equal(browserLanguage({ language: '' }), 'en');
});

test('without a browser the page stays in its own Turkish', () => {
  assert.equal(browserLanguage(undefined), 'tr');
  assert.equal(browserLanguage({ language: 'en-US', userAgent: 'Node.js/22' }), 'tr');
  // These unit tests run under Node's navigator, so the dictionaries they import are Turkish.
  assert.equal(language, 'tr');
});
