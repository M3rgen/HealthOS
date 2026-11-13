// Synthetic text at the reviewed HTML sinks, with a fresh context for each viewport.
const assert = require('node:assert/strict');
const fx = require('../../frontend/preview/fixtures.cjs');
const { BASE, launch, run, showCard, showSystemGroup, shot } = require('./lib.cjs');
const { mock, contained } = require('./helpers/accessibility.cjs');

const payload = '<img src=x onerror="window.markupUnsafe=true"> & " \'', marker = 'synthetic';

run(async () => {
  const browser = await launch();
  try {
    for (const [width, height] of [[1366, 768], [390, 844]]) {
      const page = await browser.newPage({ viewport: { width, height }, reducedMotion: 'reduce' });
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.clock.setFixedTime(new Date(fx.NOW));
      await mock(page);
      await page.route('**/api/dashboard?**', route => {
        const data = fx.dashboard(new URL(route.request().url()).searchParams);
        data.days[0].sources.steps = { source: marker, label: payload,
          alternatives: [{ source: marker, label: payload, value: 1 }] };
        return route.fulfill({ json: data });
      });
      await page.route('**/api/dashboard/measurements', route => route.fulfill({
        json: [{ ...fx.measurements[0], id: payload, metric: payload }],
      }));
      await page.route('**/sync/status', route => route.fulfill({
        json: [{ ...fx.syncStatus[0], data_type: payload, status: payload }],
      }));
      await page.route('**/api/dashboard/workouts?**', route => route.fulfill({
        json: [{ ...fx.workouts(new URL(route.request().url()).searchParams)[0],
          id: payload, source: payload, source_code: marker, exercise_type: payload, alternatives: [] }],
      }));
      await page.goto(BASE);
      await page.waitForFunction(() => document.querySelectorAll('#history-rows > tr').length === 8 &&
        (innerWidth > 900 || document.querySelector('.overview-grid').hasAttribute('data-tabbed')));
      assert.equal(await page.locator('#profile-values button[data-metric]').count(), 4);
      assert.equal(await page.locator('#history-rows .source-badge').textContent(), payload);
      // These rows are painted even with the history drawer closed. Its pointer/keyboard
      // behavior has separate checks; here inspect the sink without changing drawer state.
      assert.ok((await page.locator('.source-alternatives').first().textContent()).includes(payload));
      await contained(page);

      // Both the initial chart and its ResizeObserver redraw keep the same data points.
      await showCard(page, 'steps');
      await page.locator('.steps-card').waitFor({ state: 'visible' });
      await page.locator('.steps-card .metric-open').click();
      await page.locator('#metric-history-chart svg').waitFor();
      const titles = await page.locator('#metric-history-chart title').allTextContents();
      await page.setViewportSize({ width: width - 10, height });
      await page.waitForFunction(() => {
        const plot = document.querySelector('#metric-history-chart');
        return Number(plot.querySelector('svg').getAttribute('width')) === Math.max(280, plot.clientWidth);
      });
      assert.deepEqual(await page.locator('#metric-history-chart title').allTextContents(), titles);
      await contained(page);
      await page.keyboard.press('Escape');
      await page.locator('#metric-history-dialog').waitFor({ state: 'hidden' });

      await page.locator('[data-page="measurements"]').click();
      await page.locator('#measurement-rows .row-action').waitFor();
      assert.ok((await page.locator('#measurement-rows').textContent()).includes(payload));
      assert.equal(await page.locator('#measurement-rows .row-action').getAttribute('data-flag-id'), payload);
      await contained(page);
      await page.locator('[data-page="sports"]').click();
      await page.locator('#workout-rows .workout-entry').waitFor();
      assert.equal(await page.locator('#workout-rows h4').textContent(), payload);
      assert.equal(await page.locator('#workout-rows .workout-entry').getAttribute('data-workout-id'), payload);
      await contained(page);
      await page.locator('[data-page="system"]').click();
      // The responsive tab bar is installed on the next layout pass.
      await page.waitForFunction(() => innerWidth > 900 || document.querySelector('#system-panels').hasAttribute('data-tabbed'));
      await showSystemGroup(page, 'sync');
      await page.waitForFunction(value => document.querySelector('#sync-rows').textContent.includes(value), payload);
      assert.equal(await page.locator('#sync-rows td').nth(2).textContent(), payload);
      await contained(page);
      await showSystemGroup(page, 'status');
      await page.locator('#webhook-status-open').click();
      await page.locator('#webhook-status-details dd').first().waitFor();
      assert.ok((await page.locator('#webhook-status-details').textContent()).includes('Bilinmiyor'));
      await contained(page);
      for (const selector of ['#history-rows', '#measurement-rows', '#workout-rows', '#sync-rows', '#webhook-status-details'])
        assert.equal(await page.locator(`${selector} img, ${selector} [onerror]`).count(), 0, selector);
      assert.equal(await page.evaluate(() => Boolean(window.markupUnsafe)), false);
      assert.deepEqual(errors, []);
      await page.screenshot({ path: shot(`markup-sinks-${width}.png`) });
      await page.close();
      console.log(`PASS ${width}x${height}: escaped text/attributes, chart redraw, node diagnostics, no page overflow`);
    }
  } finally { await browser.close(); }
});
