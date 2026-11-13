// UX-04: a 32 px browser font leaves at least one complete history row on opening.
const assert = require('node:assert/strict');
const fx = require('../../frontend/preview/fixtures.cjs');
const { BASE, launch, run } = require('./lib.cjs');
const { contained } = require('./helpers/accessibility.cjs');

run(async () => {
  const browser = await launch(), failures = [];
  try {
    // The font-relative boundary and the existing physical short-screen bar stay contained too.
    for (const [width, height, font, minimum] of [
      [390, 844, 32, 1], [375, 667, 32, 1], [1024, 600, 32, 1],
      [375, 600, 32, 1], [375, 599, 32, 0], [320, 480, 16, 1], [320, 420, 16, 0],
    ]) {
      const page = await browser.newPage({ viewport: { width, height }, reducedMotion: 'reduce' });
      try {
        const errors = [];
        page.on('pageerror', error => errors.push(error.message));
        const cdp = await page.context().newCDPSession(page);
        await cdp.send('Page.setFontSizes', { fontSizes: { standard: font, fixed: Math.round(font * 0.8125) } });
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
          font: parseFloat(getComputedStyle(document.documentElement).fontSize),
          expanded: document.getElementById('history-toggle').getAttribute('aria-expanded'),
          panels: ['.topbar', '#overview', '.overview-grid', '#history-panel', '#history-panel .table-scroll'].map(selector => {
            const el = document.querySelector(selector), box = el.getBoundingClientRect();
            return { selector, top: box.top, height: box.height, display: getComputedStyle(el).display,
              scrollHeight: el.scrollHeight, clientHeight: el.clientHeight };
          }),
          rows: elements.map(el => {
            const rect = el.getBoundingClientRect();
            let top = Math.max(0, rect.top), bottom = Math.min(innerHeight, rect.bottom);
            for (let parent = el.parentElement; parent; parent = parent.parentElement) {
              if (!/(auto|scroll|hidden|clip)/.test(getComputedStyle(parent).overflowY)) continue;
              const edge = parent.getBoundingClientRect().top + parent.clientTop;
              top = Math.max(top, edge);
              bottom = Math.min(bottom, edge + parent.clientHeight);
            }
            return { height: rect.height, visible: Math.max(0, bottom - top) };
          }),
        }));
        const whole = state.rows.filter(row => row.height > 0 && Math.abs(row.height - row.visible) <= 0.5).length;
        console.log(`INFO ${width}x${height} font=${font}: ${whole} whole rows; ${JSON.stringify(state)}`);
        assert.equal(state.font, font);
        assert.equal(state.expanded, 'false');
        await contained(page);
        assert.deepEqual(errors, []);
        if (minimum ? whole < minimum : whole !== 0) failures.push(`${width}x${height} font=${font}: ${whole} whole rows`);
      } finally { await page.close(); }
    }
  } finally { await browser.close(); }
  assert.deepEqual(failures, []);
  console.log('PASS 200% browser font: at least one whole history row on opening, no page scroll');
});
