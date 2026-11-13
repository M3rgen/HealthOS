// Synthetic measurements only; each viewport gets an independent browser context.
const assert = require('node:assert/strict');
const { BASE, launch, run, shot } = require('./lib.cjs');
const { mock, contained } = require('./helpers/accessibility.cjs');
const fx = require('../../frontend/preview/fixtures.cjs');

run(async () => {
  const browser = await launch();
  try {
    for (const [width, height] of [[1366, 768], [390, 844]]) {
      const page = await browser.newPage({ viewport: { width, height }, reducedMotion: 'reduce' });
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.clock.setFixedTime(new Date(fx.NOW));
      await mock(page);
      await page.route('**/api/dashboard/measurements', route => route.fulfill({
        json: Array.from({ length: 100 }, (_, i) => ({ ...fx.measurements[0], id: `synthetic-${i}` })),
      }));
      await page.goto(BASE);
      await page.waitForFunction(() => document.documentElement.dataset.booted === 'true');
      await page.locator('[data-page="measurements"]').click();
      await page.locator('#measurements').waitFor({ state: 'visible' });
      await page.waitForFunction(() => document.querySelectorAll('#measurement-rows tr').length === 101);
      const note = page.locator('#measurement-rows tr').last();
      assert.equal(await note.textContent(), 'En yeni 100 ölçüm gösteriliyor; daha eskileri bu listede yer almıyor.');
      assert.equal(await note.locator('td').getAttribute('colspan'), '5');
      await contained(page);
      await note.scrollIntoViewIfNeeded();
      assert.ok(await note.evaluate(el => {
        const r = el.getBoundingClientRect(), panel = el.closest('.table-scroll');
        const p = panel.getBoundingClientRect();
        return panel.scrollTop > 0 && r.top >= p.top && r.bottom <= p.bottom + 1;
      }), 'the last row is reachable inside the table scroll region');
      await contained(page);
      await page.screenshot({ path: shot(`measurement-limit-${width}.png`) });
      assert.deepEqual(errors, []);
      await page.close();
      console.log(`PASS ${width}x${height}: 100 measurements, last-row notice reachable, no page overflow`);
    }
  } finally { await browser.close(); }
});
