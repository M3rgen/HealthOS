// Synthetic fixtures. A file of the page that cannot be fetched: an English text chunk leaves the
// page working in Turkish; the entry module leaves a clear screen with a reload instead of a blank
// page, in the build the app really serves (hashed chunks), and in the dev server.
const assert = require('node:assert/strict');
const fx = require('../../frontend/preview/fixtures.cjs');
const { BASE, launch, run } = require('./lib.cjs');

async function open(browser, { block, language, viewport = { width: 1366, height: 768 } }) {
  const page = await browser.newPage({ viewport, reducedMotion: 'reduce' });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  if (language) await page.addInitScript(value => localStorage.setItem('healthos.language', value), language);
  await page.route('**/*', route => {
    const url = new URL(route.request().url()), p = url.pathname;
    if (block.test(p)) return route.abort();
    if (p === '/health') return route.fulfill({ json: fx.health });
    if (p === '/api/dashboard') return route.fulfill({ json: fx.dashboard(url.searchParams) });
    if (p === '/sync/status') return route.fulfill({ json: fx.syncStatus });
    if (/^\/(api|sync|auth|google-health)\//.test(p)) return route.fulfill({ status: 404, json: {} });
    return route.continue();
  });
  await page.routeWebSocket(/\/api\/codex\/chat$/, () => {});
  await page.goto(BASE);
  return { page, errors };
}

const fits = page => page.evaluate(() => document.scrollingElement.scrollHeight <= innerHeight && document.scrollingElement.scrollWidth <= innerWidth);

run(async () => {
  const browser = await launch();
  try {
    // English cannot be fetched: Turkish on screen, the page works, the choice is kept, and the
    // user is told. No uncaught failure.
    {
      const { page, errors } = await open(browser, { language: 'en', block: /\/i18n\/en\.js|\/assets\/en-[^/]*\.js$/ });
      await page.waitForFunction(() => document.querySelectorAll('#history-rows tr').length === 7);
      assert.equal(await page.locator('#steps-label').textContent(), 'Bugünkü adım', 'Turkish text');
      assert.equal(await page.evaluate(() => document.documentElement.lang), 'tr');
      assert.equal(await page.evaluate(() => localStorage.getItem('healthos.language')), 'en', 'the choice is kept for the next load');
      await page.waitForFunction(() => document.getElementById('notice').dataset.source === 'language');
      assert.match(await page.locator('#notice-text').textContent(), /Türkçe açıldı/);
      assert.equal(await page.locator('#notice .notice-action').textContent(), 'Sayfayı yenile');
      assert.equal(await page.locator('#boot-failure').count(), 0, 'no failure screen: the page works');
      assert.equal(await fits(page), true);
      assert.deepEqual(errors, []);
      await page.close();
      console.log('PASS the English chunk cannot be fetched: the page opens in Turkish and says so');
    }
    // The entry module cannot be fetched: a clear full-page message with a reload, in the chosen language.
    for (const [language, title, label] of [[null, 'HealthOS açılamadı', 'Sayfayı yenile'], ['en', 'HealthOS could not open', 'Reload page']]) {
      for (const viewport of [{ width: 1366, height: 768 }, { width: 320, height: 256 }]) {
        const { page } = await open(browser, { language, viewport, block: /\/src\/main\.js$|\/assets\/index-[^/]*\.js$/ });
        await page.locator('#boot-failure').waitFor();
        assert.equal(await page.locator('#boot-failure h1').textContent(), title);
        assert.equal(await page.locator('#boot-failure button').textContent(), label);
        assert.equal(await page.evaluate(() => document.activeElement.textContent), label, 'focus is on the reload');
        assert.equal(await fits(page), true, 'the message does not scroll the document');
        const box = await page.locator('#boot-failure button').boundingBox();
        assert.ok(box.y + box.height <= viewport.height, `reload on screen at ${viewport.width}x${viewport.height}`);
        await page.close();
      }
    }
    console.log('PASS the entry module cannot be fetched: a failure screen with a reload');
  } finally { await browser.close(); }
});
