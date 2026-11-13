// Synthetic dashboard failure alongside a sync failure; no focus/visibility-dependent polling.
const assert = require('node:assert/strict');
const { BASE, launch, run } = require('./lib.cjs');
const { mock, contained } = require('./helpers/accessibility.cjs');
const fx = require('../../frontend/preview/fixtures.cjs');

run(async () => {
  const browser = await launch();
  try {
    for (const viewport of [{ width: 1366, height: 768 }, { width: 390, height: 844 }]) {
      const page = await browser.newPage({ viewport, timezoneId: 'UTC', reducedMotion: 'reduce' });
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      let failed = false;
      await page.clock.setFixedTime(new Date(fx.NOW));
      await mock(page);
      await page.route('**/health', route => route.fulfill({ json: { ...fx.health, failed_syncs: 1 } }));
      await page.route('**/sync/status', route => route.fulfill({ json: [
        { ...fx.syncStatus[0], data_type: 'steps', status: 'failed', last_error: 'rate_limited' },
      ] }));
      await page.route('**/api/dashboard?*', route => failed
        ? route.fulfill({ status: 503, json: { detail: 'Synthetic outage' } })
        : route.fulfill({ json: fx.dashboard(new URL(route.request().url()).searchParams) }));
      await page.goto(BASE);
      await page.waitForFunction(() => document.documentElement.dataset.booted === 'true' &&
        document.querySelector('#notice').dataset.source === 'sync');
      await page.waitForFunction(() => matchMedia('(max-width: 56.25em), (max-height: 30em)').matches ===
        document.querySelector('.overview-grid').hasAttribute('data-tabbed'));
      const caption = await page.locator('#range-caption').textContent();
      const steps = await page.locator('#steps-value').textContent();
      await page.locator('#notice-close').click();
      failed = true;
      // The retry event directly invokes refreshPage(), even if another browser check has focus.
      const refresh = () => page.evaluate(() => document.dispatchEvent(new Event('healthos:retry')));
      await refresh();
      await page.waitForFunction(() => document.querySelector('.metric-cards[data-stale]') ||
        document.querySelector('#notice').dataset.source === 'sync');
      const result = await page.evaluate(() => ({ notice: document.querySelector('#notice-text').textContent,
        source: document.querySelector('#notice').dataset.source,
        stale: document.querySelector('.metric-cards').hasAttribute('data-stale'),
        caption: document.querySelector('#range-caption').textContent }));
      console.log(`${viewport.width}×${viewport.height}: ${JSON.stringify(result)}`);
      assert.equal(result.source, 'dashboard');
      assert.match(result.notice, /Veriler alınamadı/);
      assert.equal(result.stale, true);
      assert.match(result.caption, / · güncellenemedi, son veri \d{2}:\d{2}$/);
      assert.equal(await page.locator('#steps-value').textContent(), steps);
      await contained(page);
      assert.equal(await page.evaluate(() => document.scrollingElement.scrollHeight), viewport.height);
      await page.clock.setFixedTime(new Date(new Date(fx.NOW).getTime() + 120000));
      failed = false;
      await page.locator('#notice .notice-action').click();
      await page.waitForFunction(() => document.querySelector('#notice').dataset.source === 'sync');
      assert.equal(await page.locator('.metric-cards[data-stale]').count(), 0);
      assert.equal(await page.locator('#range-caption').textContent(), caption);
      await contained(page);
      assert.deepEqual(errors, []);
      await page.close();
    }
    console.log('PASS stale dashboard: dashboard notice wins, old data is labelled, recovery restores sync notice; desktop/mobile contained');
  } finally { await browser.close(); }
});
