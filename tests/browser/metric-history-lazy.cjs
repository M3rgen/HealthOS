// The first history interaction must survive a delayed chart chunk and changes made meanwhile.
const assert = require('node:assert/strict');
const fx = require('../../frontend/preview/fixtures.cjs');
const { BASE, launch, run, showCard, until } = require('./lib.cjs');
const { mock, contained, visibleFocus } = require('./helpers/accessibility.cjs');

run(async () => {
  const browser = await launch();
  try {
    for (const [width, height, locale] of [[1440, 900, 'tr-TR'], [390, 844, 'en-US']]) {
      const page = await browser.newPage({ viewport: { width, height }, locale, reducedMotion: 'reduce' });
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.clock.setFixedTime(new Date(fx.NOW));
      await mock(page);
      let requested = 0, release;
      const gate = new Promise(resolve => { release = resolve; });
      await page.route(/\/assets\/metric-history-content-[^/]+\.js$/, async route => {
        requested++;
        await gate;
        await route.continue();
      });
      try {
        await page.goto(BASE);
        await page.waitForFunction(() => document.querySelectorAll('#history-rows tr').length === 7 &&
          (innerWidth > 900 || document.querySelector('.overview-grid').hasAttribute('data-tabbed')));
        assert.equal(requested, 0, 'chart is absent from the first paint');
        await contained(page);
        await showCard(page, 'steps');
        await page.locator('.steps-card .metric-open').click();
        await page.locator('#metric-history-dialog[open]').waitFor();
        await until(() => requested === 1, 'first interaction requests the chart chunk');
        await page.locator('#metric-history-status').waitFor({ state: 'visible' });
        assert.equal(await page.locator('#metric-history-chart svg').count(), 0);
        await page.locator('[data-history-tab="heart"]').click();
        await page.keyboard.press('Escape');
        await page.locator('#metric-history-dialog').waitFor({ state: 'hidden' });
        await visibleFocus(page);
        await page.locator('.steps-card .metric-open').click();
        await page.locator('#metric-history-dialog[open]').waitFor();
        await page.locator('[data-history-tab="sleep"]').click();
        release();
        await page.locator('#metric-history-chart svg').waitFor();
        await page.locator('#metric-history-days li').last().waitFor();
        assert.equal(await page.locator('[data-history-tab="sleep"]').getAttribute('aria-pressed'), 'true');
        assert.match(await page.locator('#metric-history-title').textContent(), locale === 'tr-TR' ? /Uyku/ : /Sleep/);
        await visibleFocus(page);
        await contained(page);
        await page.setViewportSize({ width: width - 20, height });
        await page.waitForFunction(() => {
          const plot = document.querySelector('#metric-history-chart');
          return Number(plot.querySelector('svg').getAttribute('width')) === Math.max(280, Math.round(plot.clientWidth));
        });
        await page.keyboard.press('Escape');
        await page.locator('#metric-history-dialog').waitFor({ state: 'hidden' });
        await page.locator('.steps-card .metric-open').click();
        await page.locator('#metric-history-chart svg').waitFor();
        await page.locator('#metric-history-days li').last().waitFor();
        assert.equal(requested, 1, 'subsequent opens reuse the chart module');
        assert.match(await page.locator('#metric-history-title').textContent(), locale === 'tr-TR' ? /Adım/ : /Step/);
        await contained(page);
        assert.deepEqual(errors, []);
        console.log(`PASS ${width}x${height} ${locale}: deferred chart, first click, tab/close race, resize, reopen; no page scroll`);
      } finally {
        release();
        await page.close();
      }
    }
  } finally { await browser.close(); }
});
