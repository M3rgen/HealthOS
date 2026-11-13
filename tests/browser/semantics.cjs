// Synthetic fixtures only; no live account records.
// Every page has one real <h1>, heading levels never skip one, and data tables mark their header cells.
const assert = require('node:assert/strict');
const fx = require('../../frontend/preview/fixtures.cjs');
const { BASE, launch, run } = require('./lib.cjs');

run(async () => {
  const browser = await launch();
  try {
    const page = await browser.newPage({ viewport: { width: 1366, height: 768 }, reducedMotion: 'reduce' });
    await page.route('**/*', route => {
      const url = new URL(route.request().url()), p = url.pathname, q = url.searchParams;
      const json = body => route.fulfill({ json: typeof body === 'function' ? body(q) : body });
      if (p === '/health') return json(fx.health);
      if (p === '/api/dashboard') return json(fx.dashboard);
      if (p === '/api/settings/appearance') return json(fx.appearanceSettings);
      if (/^\/(api|sync|auth|google-health)\//.test(p)) return route.fulfill({ status: 404, json: {} });
      return route.continue();
    });
    await page.routeWebSocket(/\/api\/codex\/chat$/, () => {});
    await page.goto(BASE);
    await page.waitForFunction(() => document.querySelectorAll('#history-rows tr').length === 7);
    for (const name of ['overview', 'measurements', 'system', 'sports']) {
      await page.locator(`[data-page="${name}"]`).click();
      await page.waitForTimeout(100);
      const levels = await page.evaluate(() => [...document.querySelectorAll('main h1, main h2, main h3, main h4')]
        .filter(h => h.closest('.page') && !h.closest('.page').hidden).map(h => [Number(h.tagName[1]), h.textContent.trim()]));
      assert.equal(levels.filter(([level]) => level === 1).length, 1, `${name}: exactly one h1`);
      levels.reduce((previous, [level, text]) => {
        assert.ok(level <= previous + 1, `${name}: "${text}" skips a heading level`);
        return level;
      }, 0);
    }
    assert.equal(await page.locator('#overview h1').textContent(), 'Genel bakış');
    assert.deepEqual(await page.evaluate(() => [...document.querySelectorAll('table thead th')].filter(th => !th.hasAttribute('scope')).map(th => th.textContent.trim())), [], 'a table header cell has no scope');
    console.log('PASS every page has one h1, no skipped heading level, every table header has a scope');
    await page.close();
  } finally {
    await browser.close();
  }
});
