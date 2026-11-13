// Disabled sources stay visible as disconnected; no missing Google route is called.
const assert = require('node:assert/strict');
const { BASE, launch, run } = require('./lib.cjs');
const fx = require('../../frontend/preview/fixtures.cjs');

run(async () => {
  const browser = await launch();
  try {
    for (const [width, height] of [[1280, 800], [390, 844], [320, 640]]) {
      for (const sources of [[], ['garmin', 'apple']]) {
        const page = await browser.newPage({ viewport: { width, height } });
        const googleCalls = [];
        let releaseHealth;
        const healthReady = new Promise(resolve => { releaseHealth = resolve; });
        await page.route('**/health', async route => {
          await healthReady;
          return route.fulfill({
            json: { ...fx.health, status: 'healthy', configuration: 'healthy', active_sources: sources },
          });
        });
        await page.route('**/api/dashboard?**', route => route.fulfill({
          json: fx.dashboard(new URL(route.request().url()).searchParams),
        }));
        for (const pattern of ['**/sync/**', '**/api/sync/**', '**/api/webhooks/**', '**/google-health/**']) {
          await page.route(pattern, route => {
            googleCalls.push(route.request().url());
            return route.fulfill({ status: 404, json: {} });
          });
        }
        await page.route('**/api/devices', route => route.fulfill({
          json: { devices: [], google: 'not_queried', thresholds: { connected_hours: 48, delayed_days: 7 } },
        }));
        await page.goto(BASE);
        await page.locator('[data-page="system"]').click();
        releaseHealth();
        await page.waitForFunction(() => document.getElementById('sync-button').disabled);
        assert(await page.locator('#webhook-info').textContent().then(text => text.includes('Google bağlı değil')));
        const fits = async () => assert(await page.evaluate(() => {
          const root = document.scrollingElement;
          return root.scrollHeight <= innerHeight && root.scrollWidth <= innerWidth;
        }), `${width}x${height}: main page must fit`);
        await fits();
        await page.locator('[data-page="system"]').click();
        assert.equal(await page.locator('[href="/auth/google/start"]').isVisible(), false);
        assert.equal(await page.locator('#coverage-summary').textContent(), 'Google bağlı değil.');
        assert.equal(await page.locator('#webhook-status-open').isDisabled(), true);
        assert.equal(await page.locator('.coverage-panel').isVisible(), false, 'no Google coverage panel without Google');
        assert.equal(await page.locator('.webhook-status-panel').isVisible(), false, 'no Google notifications panel without Google');
        const details = await page.locator('#system-details').textContent();
        assert(details.includes('Google yapılandırmasıBağlı değil'));
        assert(details.includes(sources.length ? 'GarminSeçili' : 'GarminBağlı değil'));
        await fits();
        await page.locator('[data-page="overview"]').click();
        await page.locator('#history-toggle').click();
        await fits();
        assert.deepEqual(googleCalls, []);
        await page.close();
      }
      console.log(`PASS ${width}x${height}: source selection without page scrolling`);
    }
  } finally { await browser.close(); }
});
