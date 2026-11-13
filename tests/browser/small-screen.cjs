// Synthetic fixtures; no live account records.
// A4 small screens: at ≤900 px the overview shows one card at a time behind a tab bar instead of a
// column that scrolls inside the page; wider screens keep every card and no tab bar. Every page
// fits 375×667 and 768×1024 (and the usual desktop/phone sizes) without the main page scrolling.
const assert = require('node:assert/strict');
const fx = require('../../frontend/preview/fixtures.cjs');
const { BASE, launch, run, shot } = require('./lib.cjs');
const { contained } = require('./helpers/accessibility.cjs');

const CARDS = { profile: '.profile-panel', connection: '.connection', steps: '.steps-card', sleep: '.sleep-card', heart: '.heart-card' };
const SIZES = [
  { width: 375, height: 667, tabs: true },
  { width: 768, height: 1024, tabs: true },
  { width: 390, height: 844, tabs: true },
  { width: 1280, height: 720, tabs: false },
];

async function open(browser, viewport) {
  const page = await browser.newPage({ viewport, deviceScaleFactor: 1, reducedMotion: 'reduce', hasTouch: viewport.width < 600 });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.clock.setFixedTime(new Date(fx.NOW));
  await page.route('**/*', route => {
    const url = new URL(route.request().url());
    const p = url.pathname, q = url.searchParams;
    const json = body => route.fulfill({ json: typeof body === 'function' ? body(q) : body });
    if (p === '/health') return json(fx.health);
    if (p === '/api/dashboard') return json(fx.dashboard);
    if (p === '/api/dashboard/history') return json(fx.history);
    if (p === '/api/dashboard/measurements') return json(fx.measurements);
    if (p === '/api/dashboard/workouts') return json(fx.workouts);
    if (p === '/api/sync/coverage') return json(fx.coverage);
    if (p === '/api/webhooks/status') return json(fx.webhookStatus);
    if (p === '/api/devices') return json(fx.devices);
    if (p === '/sync/status') return json(fx.syncStatus);
    if (p === '/api/settings/appearance') return json(fx.appearanceSettings);
    if (/^\/(api|sync|auth|google-health)\//.test(p)) return route.fulfill({ status: 404, json: {} });
    return route.continue();
  });
  await page.routeWebSocket(/\/api\/codex\/chat$/, () => {});
  await page.goto(BASE);
  await page.waitForFunction(() => document.querySelectorAll('#history-rows tr').length === 7);
  return { page, errors };
}

// The visible card fills its slot between the tab bar and the history panel, inside the viewport.
async function cardFits(page, selector) {
  const box = await page.locator(selector).evaluate(el => {
    const r = el.getBoundingClientRect(), history = document.getElementById('history-panel').getBoundingClientRect();
    return { top: r.top, bottom: r.bottom, height: r.height, historyTop: history.top, scrolls: el.scrollHeight > el.clientHeight + 1 };
  });
  assert.ok(box.height > 100, `${selector} is squeezed: ${JSON.stringify(box)}`);
  assert.ok(box.bottom <= box.historyTop + 1, `${selector} runs under the history panel: ${JSON.stringify(box)}`);
  return box;
}

run(async () => {
  const browser = await launch();
  try {
    for (const viewport of SIZES) {
      const name = `${viewport.width}x${viewport.height}`;
      const { page, errors } = await open(browser, viewport);
      const tablist = page.locator('.overview-tabs[role="tablist"]');
      if (!viewport.tabs) {
        assert.equal(await tablist.isVisible(), false, 'no tab bar on wide screens');
        for (const selector of Object.values(CARDS)) assert.ok(await page.locator(selector).isVisible(), `${selector} shows on wide screens`);
        assert.equal(await page.locator('.profile-panel').getAttribute('role'), null);
        assert.equal(await page.locator('.connection').getAttribute('aria-labelledby'), 'connection-title');
        await contained(page);
        await page.screenshot({ path: shot(`small-screen-${name}-overview.png`) });
        console.log(`PASS ${name}: every card, no tabs, no page scroll`);
        await page.close();
        continue;
      }
      assert.ok(await tablist.isVisible(), 'tab bar on narrow screens');
      // The overview no longer scrolls as a column: the grid itself has nothing to scroll.
      const grid = await page.locator('.overview-grid').evaluate(el => ({ overflow: getComputedStyle(el).overflowY, scroll: el.scrollHeight - el.clientHeight }));
      assert.notEqual(grid.overflow, 'auto');
      assert.ok(grid.scroll <= 1, `overview grid scrolls by ${grid.scroll}px`);
      assert.equal(await page.locator('#overview-tab-steps').getAttribute('aria-selected'), 'true');
      for (const [key, selector] of Object.entries(CARDS)) {
        await page.locator(`#overview-tab-${key}`).click();
        for (const [other, otherSelector] of Object.entries(CARDS))
          assert.equal(await page.locator(otherSelector).isVisible(), other === key, `${key}: ${otherSelector} visibility`);
        const panel = page.locator(selector);
        assert.equal(await panel.getAttribute('role'), 'tabpanel');
        assert.equal(await panel.getAttribute('aria-labelledby'), `overview-tab-${key}`);
        assert.equal(await page.locator(`#overview-tab-${key}`).getAttribute('aria-controls'), await panel.getAttribute('id'));
        const box = await cardFits(page, selector);
        // On the tablet size every card fits without even scrolling inside itself.
        if (viewport.width >= 768) assert.equal(box.scrolls, false, `${selector} scrolls at ${name}`);
        await contained(page);
        await page.screenshot({ path: shot(`small-screen-${name}-${key}.png`) });
      }
      // The profile's actions are reachable inside its card at the smallest size too.
      await page.locator('#overview-tab-profile').click();
      const add = await page.locator('#add-measurement').evaluate(el => {
        const r = el.getBoundingClientRect(), card = el.closest('.profile-panel').getBoundingClientRect();
        return r.bottom <= card.bottom + 1 && r.top >= card.top;
      });
      assert.ok(add, '"Ölçüm ekle" is inside the visible profile card');
      // The 3D model (or its silhouette) comes back with a real size after another tab was shown.
      await page.locator('#overview-tab-heart').click();
      await page.locator('#overview-tab-profile').click();
      const model = await page.locator('#body-model').boundingBox();
      assert.ok(model.width > 80 && model.height > 120, `body model box ${JSON.stringify(model)}`);

      // Keyboard: arrows move between tabs and show the card; Home/End jump; focus stays visible.
      await page.locator('#overview-tab-profile').focus();
      await page.keyboard.press('ArrowRight');
      assert.equal(await page.evaluate(() => document.activeElement.id), 'overview-tab-connection');
      assert.ok(await page.locator('.connection').isVisible());
      await page.keyboard.press('End');
      assert.equal(await page.evaluate(() => document.activeElement.id), 'overview-tab-heart');
      await page.keyboard.press('ArrowRight');
      assert.equal(await page.evaluate(() => document.activeElement.id), 'overview-tab-profile');
      await page.keyboard.press('ArrowLeft');
      assert.equal(await page.evaluate(() => document.activeElement.id), 'overview-tab-heart');
      assert.deepEqual(await page.locator('.overview-tab').evaluateAll(tabs => tabs.map(tab => tab.tabIndex)), [-1, -1, -1, -1, 0]);

      // The expanded history sheet makes the tab bar inert along with the cards.
      await page.locator('#history-toggle').click();
      await page.waitForFunction(() => document.querySelector('#history-panel.expanded'));
      assert.equal(await page.locator('.overview-grid').evaluate(el => el.inert), true);
      await contained(page);
      await page.locator('#history-toggle').click();
      await page.waitForFunction(() => !document.querySelector('#history-panel.expanded'));

      // Crossing the breakpoint restores every card (and back).
      await page.setViewportSize({ width: 1280, height: 720 });
      await page.waitForFunction(() => !document.querySelector('.overview-grid').hasAttribute('data-tabbed'));
      for (const selector of Object.values(CARDS)) assert.ok(await page.locator(selector).isVisible(), `${selector} after widening`);
      assert.equal(await tablist.isVisible(), false);
      await contained(page);
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      await page.waitForFunction(() => document.querySelector('.overview-grid').hasAttribute('data-tabbed'));
      assert.ok(await page.locator('.heart-card').isVisible());
      assert.equal(await page.locator('.profile-panel').isVisible(), false);

      // The other pages and the dialogs fit these sizes too.
      for (const view of ['measurements', 'system', 'sports']) {
        await page.locator(`[data-page="${view}"]`).click();
        await page.locator(`#${view}`).waitFor();
        await page.waitForTimeout(200);
        await contained(page);
        await page.screenshot({ path: shot(`small-screen-${name}-${view}.png`) });
      }
      await page.locator('[data-page="overview"]').click();
      await page.locator('#overview-tab-steps').click();
      await page.locator('.steps-card .metric-open, .steps-card').first().click();
      await page.locator('#metric-history-dialog[open]').waitFor();
      await contained(page);
      await page.screenshot({ path: shot(`small-screen-${name}-metric-history.png`) });
      assert.deepEqual(errors, []);
      console.log(`PASS ${name}: one card per tab, cards fit above the history, keyboard tabs, no page scroll on any page`);
      await page.close();
    }
  } finally { await browser.close(); }
});
