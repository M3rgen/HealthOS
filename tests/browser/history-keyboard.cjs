// Synthetic fixtures. The history handle resizes by keyboard in steps: ArrowUp grows, ArrowDown
// shrinks down to closed, Enter still opens and closes it whole. The page itself never scrolls.
const assert = require('node:assert/strict');
const fx = require('../../frontend/preview/fixtures.cjs');
const { BASE, launch, run } = require('./lib.cjs');
const { contained } = require('./helpers/accessibility.cjs');

run(async () => {
  const browser = await launch();
  try {
    for (const viewport of [{ width: 1280, height: 800 }, { width: 375, height: 667 }]) {
      const page = await browser.newPage({ viewport, reducedMotion: 'reduce' });
      const errors = []; page.on('pageerror', error => errors.push(error.message));
      await page.clock.setFixedTime(new Date(fx.NOW));
      await page.route('**/*', route => {
        const url = new URL(route.request().url());
        const p = url.pathname;
        if (p === '/health') return route.fulfill({ json: fx.health });
        if (p === '/api/dashboard') return route.fulfill({ json: fx.dashboard(url.searchParams) });
        if (/^\/(api|sync|auth|google-health)\//.test(p)) return route.fulfill({ status: 404, json: {} });
        return route.continue();
      });
      await page.routeWebSocket(/\/api\/codex\/chat$/, () => {});
      await page.goto(BASE);
      await page.waitForFunction(() => document.querySelectorAll('#history-rows tr').length === 7);
      const toggle = page.locator('#history-toggle');
      const panel = page.locator('#history-panel');
      const top = async () => (await panel.boundingBox()).y;
      const start = await top();
      await toggle.focus();
      await page.keyboard.press('ArrowDown');
      assert.equal(await top(), start, 'ArrowDown on a closed panel does nothing');
      await page.keyboard.press('ArrowUp');
      assert.equal(await toggle.getAttribute('aria-expanded'), 'true');
      const first = await top();
      assert.ok(first < start, `ArrowUp opens by a step: ${start} → ${first}`);
      await page.keyboard.press('ArrowUp');
      const second = await top();
      assert.ok(second < first, `a second step grows: ${first} → ${second}`);
      assert.ok(first - second < (start - second), 'one step, not the whole height');
      await contained(page);
      await page.keyboard.press('ArrowDown');
      assert.ok(Math.abs(await top() - first) < 2, 'ArrowDown reverses the step');
      await page.keyboard.press('ArrowDown');
      await page.waitForFunction(() => document.getElementById('history-toggle').getAttribute('aria-expanded') === 'false');
      assert.equal(await panel.evaluate(el => el.classList.contains('expanded')), false, 'it closes at zero');
      assert.equal(await page.evaluate(() => document.activeElement.id), 'history-toggle');
      await page.keyboard.press('Enter');
      await page.waitForFunction(() => document.getElementById('history-toggle').getAttribute('aria-expanded') === 'true');
      await page.keyboard.press('Enter');
      await page.waitForFunction(() => document.getElementById('history-toggle').getAttribute('aria-expanded') === 'false');
      await contained(page);
      assert.deepEqual(errors, []);
      console.log(`PASS ${viewport.width}x${viewport.height}: arrow keys resize the history panel step by step`);
      await page.close();
    }
  } finally { await browser.close(); }
});
