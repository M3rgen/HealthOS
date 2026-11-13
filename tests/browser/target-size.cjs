// Synthetic fixtures; each page opens directly without relying on prior navigation or focus.
const assert = require('node:assert/strict');
const { BASE, launch, run } = require('./lib.cjs');
const { mock, contained } = require('./helpers/accessibility.cjs');
const fx = require('../../frontend/preview/fixtures.cjs');

async function targets(page, name) {
  const boxes = await page.locator('a[href], button, input, select, [tabindex="0"]').evaluateAll(elements => elements.flatMap(el => {
    const r = el.getBoundingClientRect();
    if (!el.checkVisibility() || r.bottom <= 0 || r.top >= innerHeight || r.right <= 0 || r.left >= innerWidth) return [];
    if (el.matches('a[href]') && getComputedStyle(el).display === 'inline') return [];
    return [{ id: el.id || el.outerHTML.slice(0, 120), width: r.width, height: r.height }];
  }));
  assert(boxes.length > 0, `${name}: controls were measured`);
  assert.deepEqual(boxes.filter(r => r.width < 24 || r.height < 24), [], `${name}: every visible target must be at least 24×24 px`);
  await contained(page);
  console.log(`PASS ${name}: ${boxes.length} targets ≥24×24 px, no page scroll`);
  return boxes;
}
run(async () => {
  const browser = await launch();
  try {
    for (const [width, height] of [[1440, 900], [1366, 768]]) {
      const page = await browser.newPage({ viewport: { width, height }, reducedMotion: 'reduce' });
      await page.clock.setFixedTime(new Date(fx.NOW));
      await mock(page);
      for (const [view, ready] of [
        ['overview', '#history-rows tr'], ['measurements', '#measurement-rows tr'],
        ['sports', '.workout-entry'], ['system', '#sync-rows tr'],
      ]) {
        await page.goto(`${BASE}/#/${view}`);
        await page.locator(`#${view}`).waitFor({ state: 'visible' });
        await page.locator(ready).first().waitFor({ state: 'visible' });
        const boxes = await targets(page, `${width}x${height} ${view}`);
        if (view === 'system') assert(boxes.some(r => r.id === 'model-motion'), 'the model checkbox must be measured');
      }
      await page.close();
    }
  } finally { await browser.close(); }
});
