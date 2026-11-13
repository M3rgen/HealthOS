// Synthetic fixtures; no live account records.
// Without a stored choice the interface opens in the browser's language: an English browser gets
// English, a Turkish one Turkish, one that names neither English. A choice made in Sistem › Görünüm
// (stored in this browser) wins over the browser's language either way.
const assert = require('node:assert/strict');
const fx = require('../../frontend/preview/fixtures.cjs');
const { BASE, launch, run } = require('./lib.cjs');

async function opened(browser, locale, stored) {
  const context = await browser.newContext({ locale, viewport: { width: 1280, height: 720 }, timezoneId: 'UTC', reducedMotion: 'reduce' });
  if (stored) await context.addInitScript(value => localStorage.setItem('healthos.language', value), stored);
  const page = await context.newPage();
  await page.clock.setFixedTime(new Date(fx.NOW));
  await page.route('**/*', route => {
    const url = new URL(route.request().url()), p = url.pathname;
    const json = body => route.fulfill({ json: typeof body === 'function' ? body(url.searchParams) : body });
    if (p === '/health') return json(fx.health);
    if (p === '/api/dashboard') return json(fx.dashboard);
    if (p === '/api/dashboard/history') return json(fx.history);
    if (/^\/(api|sync|auth|google-health)\//.test(p)) return route.fulfill({ status: 404, json: {} });
    return route.continue();
  });
  await page.routeWebSocket(/\/api\/codex\/chat$/, () => {});
  await page.goto(BASE);
  await page.waitForFunction(() => document.querySelectorAll('#history-rows tr').length === 7);
  const seen = await page.evaluate(() => ({
    lang: document.documentElement.lang,
    connection: document.getElementById('connection-title').textContent,
    stored: localStorage.getItem('healthos.language'),
    select: /** @type {HTMLSelectElement} */ (document.getElementById('language')).value,
  }));
  await context.close();
  return seen;
}

run(async () => {
  const browser = await launch();
  try {
    for (const [locale, stored, lang, connection] of [
      ['en-US', null, 'en', 'Data connection'],
      ['tr-TR', null, 'tr', 'Veri bağlantısı'],
      ['de-DE', null, 'en', 'Data connection'],
      ['en-US', 'tr', 'tr', 'Veri bağlantısı'],
      ['tr-TR', 'en', 'en', 'Data connection'],
    ]) {
      const seen = await opened(browser, locale, stored);
      const name = `${locale} with ${stored ?? 'no'} stored choice`;
      assert.equal(seen.lang, lang, name);
      assert.equal(seen.connection, connection, name);
      assert.equal(seen.select, lang, `${name}: Sistem › Dil shows the language on screen`);
      assert.equal(seen.stored, stored, `${name}: opening the page stores nothing`);
    }
    console.log('PASS no stored choice follows the browser language; a stored choice wins');
  } finally { await browser.close(); }
});
