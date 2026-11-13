// Synthetic fixtures: opening the overview without interaction shows at least two whole days.
const assert = require('node:assert/strict');
const fx = require('../../frontend/preview/fixtures.cjs');
const { BASE, launch, run } = require('./lib.cjs');
const { contained } = require('./helpers/accessibility.cjs');

run(async () => {
  const browser = await launch();
  try {
    for (const [width, height] of [[360, 640], [320, 568], [568, 320]]) {
      // A fresh context for each viewport: no inherited tab, focus, scroll or stored view.
      const page = await browser.newPage({ viewport: { width, height }, reducedMotion: 'reduce' });
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.clock.setFixedTime(new Date(fx.NOW));
      await page.route('**/*', route => {
        const url = new URL(route.request().url()), p = url.pathname;
        if (p === '/health') return route.fulfill({ json: fx.health });
        if (p === '/api/dashboard') return route.fulfill({ json: fx.dashboard(url.searchParams) });
        if (/^\/(api|sync|auth|google-health)\//.test(p)) return route.fulfill({ status: 404, json: {} });
        return route.continue();
      });
      await page.routeWebSocket(/\/api\/codex\/chat$/, () => {});
      await page.goto(BASE);
      await page.waitForFunction(() => document.querySelectorAll('#history-rows tr').length === 7 &&
        document.querySelector('.overview-grid[data-tabbed]') &&
        document.querySelector('.overview-tabs').getBoundingClientRect().height > 0);
      await page.evaluate(() => document.fonts.ready);
      const state = await page.locator('#history-rows tr').evaluateAll(elements => ({
        expanded: document.getElementById('history-toggle').getAttribute('aria-expanded'),
        // Narrow and short screens open on the Steps card (#473), not Profile.
        opening: ['steps', 'profile'].map(key => document.getElementById(`overview-tab-${key}`).getAttribute('aria-selected')),
        rows: elements.map(el => {
          const rect = el.getBoundingClientRect();
          let top = Math.max(0, rect.top), bottom = Math.min(innerHeight, rect.bottom);
          // Intersect the row with every ancestor's visible scrollport, including clipped panels.
          for (let parent = el.parentElement; parent; parent = parent.parentElement) {
            if (!/(auto|scroll|hidden|clip)/.test(getComputedStyle(parent).overflowY)) continue;
            const edge = parent.getBoundingClientRect().top + parent.clientTop;
            top = Math.max(top, edge);
            bottom = Math.min(bottom, edge + parent.clientHeight);
          }
          return { height: rect.height, visible: Math.max(0, bottom - top) };
        }),
      }));
      const { rows } = state;
      const whole = rows.filter(row => row.height > 0 && Math.abs(row.height - row.visible) <= 0.5).length;
      assert.ok(whole >= 2, `${width}x${height}: ${whole} whole rows; ${JSON.stringify(rows)}`);
      assert.equal(state.expanded, 'false');
      assert.deepEqual(state.opening, ['true', 'false']);
      await contained(page);
      assert.deepEqual(errors, []);
      console.log(`PASS ${width}x${height}: ${whole} whole history rows on opening, no page scroll`);
      await page.close();
    }
  } finally { await browser.close(); }
});
