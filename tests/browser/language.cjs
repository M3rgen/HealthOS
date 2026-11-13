// Synthetic fixtures; no live account records.
// A5: Turkish is the default and loads no English code; choosing English in Sistem › Görünüm
// reloads the dashboard in English (static text, dates, numbers) from a lazily loaded chunk, and
// choosing Türkçe again restores the exact Turkish page.
const assert = require('node:assert/strict');
const fx = require('../../frontend/preview/fixtures.cjs');
const { BASE, launch, run, shot, showSystemGroup } = require('./lib.cjs');
const { contained } = require('./helpers/accessibility.cjs');

const english = url => /\/assets\/en-[\w-]+\.js$|\/src\/i18n\/en\.js$/.test(new URL(url).pathname);

async function open(context) {
  const page = await context.newPage();
  const errors = [], scripts = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => { if (request.resourceType() === 'script') scripts.push(request.url()); });
  await page.clock.setFixedTime(new Date(fx.NOW));
  await page.route('**/*', route => {
    const url = new URL(route.request().url());
    const p = url.pathname, q = url.searchParams;
    const json = body => route.fulfill({ json: typeof body === 'function' ? body(q) : body });
    if (p === '/health') return json(fx.health);
    if (p === '/api/dashboard') return json(fx.dashboard);
    if (p === '/api/dashboard/history') return json(fx.history);
    if (p === '/api/sync/coverage') return json(fx.coverage);
    if (p === '/api/webhooks/status') return json(fx.webhookStatus);
    if (p === '/api/devices') return json(fx.devices);
    if (p === '/sync/status') return json(fx.syncStatus);
    if (/^\/(api|sync|auth|google-health)\//.test(p)) return route.fulfill({ status: 404, json: {} });
    return route.continue();
  });
  await page.routeWebSocket(/\/api\/codex\/chat$/, () => {});
  return { page, errors, scripts };
}
const ready = page => page.waitForFunction(() => document.querySelectorAll('#history-rows tr').length === 7);
const texts = page => page.evaluate(() => ({
  lang: document.documentElement.lang,
  title: document.title,
  nav: [...document.querySelectorAll('[data-page]')].map(button => button.textContent.trim()),
  connection: document.getElementById('connection-title').textContent,
  sync: document.querySelector('#sync-button span:last-child').textContent,
  date: document.getElementById('header-date-text').textContent,
  steps: document.getElementById('steps-value').textContent,
  history: document.getElementById('history-title').textContent,
  marked: [...document.querySelectorAll('#overview [data-i18n], .topbar [data-i18n]')].map(element => element.textContent.trim()).join('|'),
}));

run(async () => {
  const browser = await launch();
  try {
    for (const viewport of [{ width: 1280, height: 720 }, { width: 390, height: 844 }]) {
      const name = `${viewport.width}x${viewport.height}`;
      const context = await browser.newContext({ viewport, timezoneId: 'UTC', reducedMotion: 'reduce' });
      const { page, errors, scripts } = await open(context);
      await page.goto(BASE);
      await ready(page);
      const turkish = await texts(page);
      assert.equal(turkish.lang, 'tr');
      assert.deepEqual(turkish.nav, ['Genel bakış', 'Ölçümler', 'Sistem', 'Spor']);
      assert.equal(turkish.connection, 'Veri bağlantısı');
      assert.equal(turkish.date, '16 Ekim 2026');
      assert.ok(!scripts.some(english), 'Turkish must not load the English dictionary');

      // Sistem › Görünüm › Dil → English; the page reloads in English.
      await page.locator('[data-page="system"]').click();
      await showSystemGroup(page, 'settings');
      const select = page.locator('#language');
      assert.equal(await select.inputValue(), 'tr');
      await Promise.all([page.waitForEvent('load'), select.selectOption('en')]);
      await ready(page);
      const result = await texts(page);
      assert.equal(result.lang, 'en');
      assert.equal(result.title, 'System · HealthOS', 'the reload keeps the open page and names it');
      assert.deepEqual(result.nav, ['Overview', 'Measurements', 'System', 'Sports']);
      assert.equal(result.connection, 'Data connection');
      assert.equal(result.sync, 'Sync now');
      assert.equal(result.history, 'Last 7 days');
      assert.equal(result.date, '16 October 2026', 'dates follow the interface language');
      assert.match(result.steps, /^\d{1,3}(,\d{3})*$/, `English digit grouping: ${result.steps}`);
      assert.ok(scripts.some(english), 'English is loaded on demand');
      assert.equal(await page.evaluate(() => localStorage.getItem('healthos.language')), 'en');
      await contained(page);
      await page.screenshot({ path: shot(`language-en-${name}.png`) });

      // Back to Türkçe: the choice is stored (it must outlast an English browser) and the page is
      // Turkish again, unchanged.
      await page.locator('[data-page="system"]').click();
      assert.equal(await page.locator('#language').inputValue(), 'en');
      await showSystemGroup(page, 'settings');
      await Promise.all([page.waitForEvent('load'), page.locator('#language').selectOption('tr')]);
      await ready(page);
      const restored = await texts(page);
      assert.equal(restored.title, 'Sistem · HealthOS', 'the reload stays on the open page');
      assert.deepEqual({ ...restored, title: turkish.title }, turkish);
      assert.equal(await page.evaluate(() => localStorage.getItem('healthos.language')), 'tr');
      assert.deepEqual(errors, []);
      console.log(`PASS ${name}: Turkish default without English code, English on demand, back to identical Turkish`);
      await context.close();
    }
  } finally { await browser.close(); }
});
