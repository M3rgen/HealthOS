// UX-18: real keyboard navigation and layout, synthetic responses only.
const assert = require('node:assert/strict');
const { BASE, launch, run, showSystemGroup } = require('./lib.cjs');
const { tabTo, active, contained, visibleFocus } = require('./helpers/accessibility.cjs');
const fx = require('../../frontend/preview/fixtures.cjs');
const SCROLLER = '.sync-history .table-scroll';

async function allScrollStops(page) {
  await page.waitForFunction(() => [...document.querySelectorAll('[data-scroll-focus]')].every(el => {
    const over = el.clientWidth > 0 && el.clientHeight > 0 &&
      (el.scrollWidth > el.clientWidth || el.scrollHeight > el.clientHeight);
    return el.getAttribute('tabindex') === (over ? '0' : null);
  }));
  await contained(page);
}

async function expectScrollStop(page, selector, expected) {
  await page.waitForFunction(({selector, expected}) => {
    const el = document.querySelector(selector);
    const over = el.clientWidth > 0 && el.clientHeight > 0 &&
      (el.scrollWidth > el.clientWidth || el.scrollHeight > el.clientHeight);
    return over === expected && el.getAttribute('tabindex') === (expected ? '0' : null);
  }, {selector, expected});
  assert.equal(await page.locator(selector).getAttribute('role'), expected ? 'region' : null);
  assert.equal(await page.locator(selector).getAttribute('aria-label'), expected ? 'Eşitleme işlerinin durumu' : null);
}

async function skipFittingTable(page) {
  await expectScrollStop(page, SCROLLER, false);
  await allScrollStops(page);
  for (const reverse of [false, true]) {
    await page.locator('[data-page="system"]').focus();
    let visited = 0;
    do {
      await page.keyboard.press(reverse ? 'Shift+Tab' : 'Tab');
      assert.equal(await active(page, SCROLLER), false, 'a fitting table must be skipped in both Tab directions');
      await visibleFocus(page);
      assert(++visited < 100, 'Tab must return to the current navigation button');
    } while (!await active(page, '[data-page="system"]'));
  }
}

run(async () => {
  const browser = await launch();
  try {
    const page = await browser.newPage({viewport: {width: 1366, height: 768}, reducedMotion: 'reduce'});
    await page.clock.setFixedTime(new Date(fx.NOW));
    let syncRows = fx.syncStatus;
    await page.route('**/*', route => {
      const {pathname: p, searchParams: q} = new URL(route.request().url());
      const json = value => route.fulfill({json: typeof value === 'function' ? value(q) : value});
      if (p === '/health') return json(fx.health);
      if (p === '/api/dashboard') return json(fx.dashboard);
      if (p === '/sync/status') return json(syncRows);
      if (p === '/api/devices') return json(fx.devices);
      if (p === '/api/sync/coverage') return json(fx.coverage);
      if (p === '/api/webhooks/status') return json(fx.webhookStatus);
      if (p === '/api/settings/appearance') return json(fx.appearanceSettings);
      if (/^\/(api|sync|auth|google-health)\//.test(p)) return route.fulfill({status: 404, json: {}});
      return route.continue();
    });
    await page.routeWebSocket(/\/api\/codex\/chat$/, () => {});
    await page.goto(`${BASE}/#/system`);
    await page.waitForFunction(() => document.querySelectorAll('#sync-rows tr').length > 1);
    await skipFittingTable(page);

    // Resize the same page: the previously fitting table becomes keyboard-scrollable.
    // The tab bar follows a MediaQueryList `change` event, which the browser dispatches in a
    // later rendering step than setViewportSize resolves. Wait for it, or showSystemGroup sees no
    // tabs, does nothing, and the tabs then arrive with the default group and hide this table.
    await page.setViewportSize({width: 320, height: 568});
    await page.locator('#system-tab-sync').waitFor();
    await showSystemGroup(page, 'sync');
    await expectScrollStop(page, SCROLLER, true);
    await tabTo(page, SCROLLER);
    await page.keyboard.press('ArrowRight');
    await page.waitForFunction(selector => document.querySelector(selector).scrollLeft > 0, SCROLLER);
    await allScrollStops(page);
    await page.setViewportSize({width: 1366, height: 768});
    await page.locator('#system-tab-sync').waitFor({state: 'hidden'});
    await skipFittingTable(page);

    // A response can change scrollWidth without changing the scrollport's box.
    syncRows = [{...fx.syncStatus[0], data_type: 'sentetik'.repeat(100)}];
    await page.locator('#refresh-system').click();
    await expectScrollStop(page, SCROLLER, true);
    await tabTo(page, SCROLLER);
    await page.keyboard.press('ArrowRight');
    await page.waitForFunction(selector => document.querySelector(selector).scrollLeft > 0, SCROLLER);
    syncRows = fx.syncStatus;
    await page.locator('#refresh-system').click();
    await skipFittingTable(page);

    // The other table scrollports use the same helper, including vertical-only overflow.
    await page.locator('[data-page="overview"]').click();
    await page.waitForFunction(() => document.querySelectorAll('#history-rows tr').length === 7);
    await page.locator('#history-table').evaluate(el => {
      el.style.height = '100px'; el.style.maxHeight = '100px';
    });
    await page.waitForFunction(() => document.querySelector('#history-table').getAttribute('tabindex') === '0');
    await tabTo(page, '#history-table');
    await page.keyboard.press('ArrowDown');
    await page.waitForFunction(() => document.querySelector('#history-table').scrollTop > 0);
    await allScrollStops(page);
    await page.close();
    console.log('PASS fitting scrollports skip Tab; resize and table redraw restore keyboard scrolling without page overflow');
  } finally { await browser.close(); }
});
