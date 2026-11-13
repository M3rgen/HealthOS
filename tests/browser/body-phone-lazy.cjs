// Real model requests, synthetic API data, and a fresh context for each viewport.
const assert = require('node:assert/strict');
const fx = require('../../frontend/preview/fixtures.cjs');
const { BASE, launch, run, until, shot } = require('./lib.cjs');
const { contained } = require('./helpers/accessibility.cjs');

run(async () => {
  const browser = await launch();
  try {
    for (const viewport of [{ width: 390, height: 844 }, { width: 1366, height: 768 }]) {
      const page = await browser.newPage({ viewport, reducedMotion: 'reduce' });
      const models = [], errors = [];
      let release;
      const gate = new Promise(resolve => { release = resolve; });
      page.on('pageerror', error => errors.push(error.message));
      page.on('request', request => {
        if (new URL(request.url()).pathname.endsWith('.glb')) models.push(request.url());
      });
      await page.route('**/*', async route => {
        const url = new URL(route.request().url()), p = url.pathname;
        if (url.origin !== new URL(BASE).origin) return route.abort();
        if (p.endsWith('.glb')) { await gate; return route.abort(); }
        if (p === '/health') return route.fulfill({ json: fx.health });
        if (p === '/api/dashboard') return route.fulfill({ json: fx.dashboard(url.searchParams) });
        if (p === '/sync/status') return route.fulfill({ json: fx.syncStatus });
        if (/^\/(api|sync|auth|google-health)\//.test(p)) return route.fulfill({ status: 404, json: {} });
        return route.continue();
      });
      await page.routeWebSocket(/\/api\/codex\/chat/, () => {});
      try {
        await page.goto(BASE);
        await page.waitForFunction(() => document.querySelectorAll('#history-rows tr').length === 7);
        if (viewport.width === 390) {
          // Wait for the tabbed layout, then measure the complete first five seconds of the
          // navigation. Timer polling also works when a neighbour owns the foreground tab.
          await page.locator('#overview-tab-steps').waitFor({ state: 'visible' });
          assert.equal(await page.locator('#body-model').isVisible(), false);
          await page.waitForFunction(() => performance.now() >= 5000, undefined, { polling: 100 });
          assert.deepEqual(models, [], 'no GLB request during the first five seconds');
          await contained(page);
          await page.screenshot({ path: shot('body-phone-initial.png') });
          await page.locator('#overview-tab-profile').click();
          await page.locator('#body-model .model-fallback').waitFor({ state: 'visible' });
        }
        await until(() => models.length === 1, 'one model request once the figure is shown');
        assert.ok(await page.locator('#body-model .model-fallback').isVisible(), 'silhouette while the GLB response is pending');
        await page.screenshot({ path: shot(`body-visible-${viewport.width}.png`) });
        if (viewport.width === 390) {
          await page.locator('#overview-tab-steps').click();
          await page.locator('#overview-tab-profile').click();
        }
        assert.equal(models.length, 1, 'returning to Profile does not fetch again');
        await contained(page);
        assert.deepEqual(errors, []);
        console.log(`PASS ${viewport.width}×${viewport.height}: ${viewport.width === 390 ? 'no GLB for 5 s, Profile loads once' : 'desktop loads as before'}; silhouette and no page scroll`);
      } finally { release(); await page.close(); }
    }
  } finally { await browser.close(); }
});
