// Synthetic health/dashboard responses only.
const assert = require('node:assert/strict');
const { BASE, launch, run } = require('./lib.cjs');
const fx = require('../../frontend/preview/fixtures.cjs');

run(async () => {
  const browser = await launch();
  try {
    for (const [width, height] of [[1280, 800], [1280, 640], [390, 844], [320, 640]]) {
      const page = await browser.newPage({ viewport: { width, height } });
      let disabled = true;
      let releaseHealth;
      const healthReady = new Promise(resolve => { releaseHealth = resolve; });
      await page.route('**/health', async route => {
        await healthReady;
        await route.fulfill({ status: 503,
          json: { ...fx.health, database: 'unavailable', ui_login_disabled: disabled } });
      });
      await page.route('**/api/dashboard?**', route => route.fulfill({
        json: fx.dashboard(new URL(route.request().url()).searchParams) }));
      await page.route('**/sync/status', route => route.fulfill({ json: fx.syncStatus }));
      await page.goto(BASE);
      const banner = page.locator('#login-disabled-warning');
      await page.locator('#history-toggle').click();
      releaseHealth();
      await banner.waitFor({ state: 'visible' });
      assert.equal(await banner.textContent(), 'Giriş kapalı — yalnız bu bilgisayardan erişilebilir.');
      const fits = async (state = 'current view') => assert(await page.evaluate(() => {
        const root = document.scrollingElement;
        const banner = document.getElementById('login-disabled-warning').getBoundingClientRect();
        return root.scrollHeight <= innerHeight && root.scrollWidth <= innerWidth &&
          banner.top >= 0 && banner.bottom <= innerHeight;
      }), `${JSON.stringify(page.viewportSize())} ${state}: banner and page must fit`);
      const warningClear = () => page.evaluate(() => {
        window.settledFrames = 0;
        window.lastHistoryTop = undefined;
      }).then(() => page.waitForFunction(() => {
        const panel = document.getElementById('history-panel');
        const top = panel.getBoundingClientRect().top;
        // After a resize the banner re-wraps and the sheet moves a frame later; measure it settled.
        window.settledFrames = top === window.lastHistoryTop ? window.settledFrames + 1 : 0;
        window.lastHistoryTop = top;
        return window.settledFrames >= 5 && !panel.getAnimations().length && top >=
          document.getElementById('login-disabled-warning').getBoundingClientRect().bottom;
      }));
      await warningClear();
      await fits('B: health arrives after history opens');
      const warningBounds = await banner.boundingBox();
      const historyBounds = await page.locator('#history-panel').boundingBox();
      assert(historyBounds.y >= warningBounds.y + warningBounds.height, 'expanded history keeps warning visible');
      if (width === 1280 && height === 800) {
        await page.setViewportSize({ width: 390, height: 844 });
        await warningClear();
        await fits('A: open history resized to mobile with warning');
        await page.setViewportSize({ width, height });
        await warningClear();
        // A dragged sheet must also recompute its horizontal and vertical bounds.
        const expandedBounds = await page.locator('#history-panel').boundingBox();
        const handle = await page.locator('#history-toggle').boundingBox();
        await page.mouse.move(handle.x + handle.width / 2, handle.y + handle.height / 2);
        await page.mouse.down();
        await page.mouse.move(handle.x + handle.width / 2, handle.y + handle.height / 2 + 70, {steps: 8});
        const moving = await page.locator('#history-panel').boundingBox();
        assert(Math.abs(expandedBounds.height - 70 - moving.height) < 2, 'open history follows drag distance');
        await page.mouse.up();
        const released = await page.locator('#history-panel').boundingBox();
        assert(Math.abs(moving.height - released.height) < 2, 'drag retains released height');
        await page.setViewportSize({ width: 390, height: 844 });
        await warningClear();
        const mobile = await page.locator('#history-panel').boundingBox();
        assert(mobile.x >= 0 && mobile.x + mobile.width <= 390 && mobile.y + mobile.height <= 844,
          'resized dragged history stays inside mobile viewport');
        await fits('A: dragged history resized to mobile');
        await page.setViewportSize({ width, height });
        await warningClear();
      }
      await fits();
      await page.keyboard.press('Escape');
      for (const name of ['measurements', 'system', 'sports']) {
        await page.locator(`[data-page="${name}"]`).click();
        assert(await banner.isVisible(), `warning stays on ${name}`);
        await fits();
      }
      disabled = false;
      await page.reload();
      await page.waitForFunction(() => document.getElementById('service-status').textContent !== 'Kontrol ediliyor');
      assert.equal(await banner.isVisible(), false);
      if (width === 1280 && height === 800) {
        await page.locator('[data-page="overview"]').click();
        await page.locator('#history-toggle').click();
        await page.waitForFunction(() => !document.getElementById('history-panel').getAnimations().length);
        await page.setViewportSize({ width: 390, height: 844 });
        // A also occurred before the warning was introduced.
        assert(await page.evaluate(() => document.scrollingElement.scrollHeight <= innerHeight &&
          document.scrollingElement.scrollWidth <= innerWidth), 'A: open history resized without warning');
      }
      await page.close();
      console.log(`PASS ${width}x${height}: persistent login warning without page scrolling`);
    }
  } finally { await browser.close(); }
});
