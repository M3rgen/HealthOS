// Synthetic preview: actions remain on screen from opening through keyboard traversal.
const assert = require('node:assert/strict');
const { BASE, launch, run } = require('./lib.cjs');
const fx = require('../../frontend/preview/fixtures.cjs');
const floor = require('./viewport-floors.json').sizes.reduce((a, b) => a[0] * a[1] < b[0] * b[1] ? a : b);

run(async () => {
  const browser = await launch();
  try {
    for (const [width, height] of [[1440, 900], [1024, 600], [1366, 768], [390, 844], floor]) {
      const page = await browser.newPage({ viewport: { width, height }, reducedMotion: 'reduce' });
      await page.clock.setFixedTime(new Date(fx.NOW));
      await page.route('**/*', route => {
        const url = new URL(route.request().url()), p = url.pathname;
        if (url.origin !== new URL(BASE).origin) return route.abort();
        const data = { '/health': fx.health, '/api/dashboard': fx.dashboard,
          '/api/dashboard/history': fx.history, '/api/dashboard/workouts': fx.workouts,
          '/sync/status': fx.syncStatus, '/api/settings/appearance': fx.appearanceSettings }[p];
        if (data) return route.fulfill({ json: typeof data === 'function' ? data(url.searchParams) : data });
        if (/^\/(api|sync)\//.test(p)) return route.fulfill({ json: {} });
        return route.continue();
      });
      await page.routeWebSocket(/\/api\/codex\/chat$/, () => {});
      await page.goto(BASE);
      await page.waitForFunction(() => document.querySelectorAll('#history-rows tr').length === 7);
      await page.evaluate(() => document.fonts.ready);
      await page.locator('[data-page="sports"]').click();
      await page.waitForFunction(() => document.querySelector('#workout-timeline').getAttribute('aria-busy') === 'false');
      await page.locator('#workout-add').click();
      const fits = async (selector) => {
        const bounds = await page.locator(selector).evaluate(el => {
          const r = el.getBoundingClientRect(), p = el.closest('.workouts-panel').getBoundingClientRect();
          const hit = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
          return { inside: r.width > 0 && r.height > 0 && r.left >= Math.max(0, p.left)
            && r.right <= Math.min(innerWidth, p.right) && r.top >= Math.max(0, p.top)
            && r.bottom <= Math.min(innerHeight, p.bottom), hit: el === hit || el.contains(hit),
          rect: [r.top, r.bottom], panel: [p.top, p.bottom] };
        });
        assert(bounds.inside && bounds.hit, `${width}x${height} ${selector}: ${JSON.stringify(bounds)}`);
      };
      const actions = async () => {
        for (const id of ['#workout-save', '#workout-cancel']) await fits(id);
        assert.deepEqual(await page.evaluate(() => [document.scrollingElement.scrollHeight,
          document.scrollingElement.scrollWidth]), [height, width], 'no main-page scroll');
      };
      await actions();
      await fits('#workout-editor summary');
      assert.equal(await page.locator('[name="exercise_type"]').evaluate(el => document.activeElement === el), true);
      // datetime-local has several native Tab stops; traverse rather than focusing the last field.
      const last = page.locator('[name="distance_km"]');
      for (let i = 0; i < 30 && !await last.evaluate(el => document.activeElement === el); i++) {
        await page.keyboard.press('Tab');
        await actions();
      }
      assert.equal(await last.evaluate(el => document.activeElement === el), true, 'Tab reaches the final field');
      await fits('[name="distance_km"]');
      await actions();
      console.log(`PASS ${width}x${height}: title and actions visible on opening; final field reachable with Tab; no page scroll`);
      await page.close();
    }
  } finally { await browser.close(); }
});
