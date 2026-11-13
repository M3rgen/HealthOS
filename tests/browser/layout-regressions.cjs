// Synthetic fixtures. Findings that were each measured on the real page: the selected day stays in
// the top bar at a 200 % browser font, a list stays a list, a non-scrolling page is no Tab stop,
// the measurement dialog keeps its Save button on a short screen, the top menu is whole at 320×256.
const assert = require('node:assert/strict');
const fx = require('../../frontend/preview/fixtures.cjs');
const { BASE, launch, run } = require('./lib.cjs');

async function open(browser, viewport, font) {
  const page = await browser.newPage({ viewport, reducedMotion: 'reduce', hasTouch: viewport.width <= 900 });
  if (font) {
    const cdp = await page.context().newCDPSession(page);
    await cdp.send('Page.setFontSizes', { fontSizes: { standard: font, fixed: Math.round(font * 0.8125) } });
  }
  await page.clock.setFixedTime(new Date(fx.NOW));
  await page.route('**/*', route => {
    const url = new URL(route.request().url()), p = url.pathname;
    const json = body => route.fulfill({ json: typeof body === 'function' ? body(url.searchParams) : body });
    if (p === '/health') return json(fx.health);
    if (p === '/api/dashboard') return json(fx.dashboard);
    if (p === '/api/dashboard/history') return json(fx.history);
    if (p === '/api/dashboard/measurements') return json(fx.measurements);
    if (p === '/api/dashboard/workouts') return json(fx.workouts);
    if (p === '/api/sync/coverage') return json(fx.coverage);
    if (p === '/api/webhooks/status') return json(fx.webhookStatus);
    if (p === '/api/devices') return json(fx.devices);
    if (p === '/sync/status') return json(fx.syncStatus);
    if (/^\/(api|sync|auth|google-health)\//.test(p)) return route.fulfill({ status: 404, json: {} });
    return route.continue();
  });
  await page.routeWebSocket(/\/api\/codex\/chat$/, () => {});
  await page.goto(BASE);
  await page.waitForFunction(() => document.querySelectorAll('#history-rows tr').length === 7);
  return page;
}
const open_page = (page, view) => page.evaluate(id => document.querySelector(`[data-page="${id}"]`).click(), view);
const visibleText = (page, selector) => page.locator(selector).evaluate(el => {
  const r = el.getBoundingClientRect();
  return r.width > 8 && r.height > 8 && getComputedStyle(el).display !== 'none' && r.left >= 0 && r.right <= innerWidth + 0.5 ? el.textContent : null;
});

run(async () => {
  const browser = await launch();
  try {
    // The selected day: full at wide sizes, short on narrow short ones, at the default font and at 200 %.
    for (const [width, height, font, full] of [
      [1920, 1080, 32, true], [1366, 768, 32, true], [1024, 600, 32, true], [844, 390, 32, false],
      [844, 390, undefined, true], [640, 360, undefined, true], [1280, 500, undefined, true]]) {
      const page = await open(browser, { width, height }, font);
      const [shown, other] = full ? ['#header-date-text', '#header-date-short'] : ['#header-date-short', '#header-date-text'];
      const text = await visibleText(page, shown);
      assert.match(text ?? '', /\d/, `${width}x${height} font ${font}: selected day missing (${shown})`);
      assert.equal(await page.locator(other).isVisible(), false);
      assert.match(await page.locator('#header-date').getAttribute('aria-label'), /16 Ekim 2026/);
      await page.close();
    }
    // The wordmark returns where width allows.
    for (const [width, height, font, shown] of [[1920, 1080, 32, true], [1366, 768, 32, false], [1280, 500, undefined, true]]) {
      const page = await open(browser, { width, height }, font);
      assert.equal(await visibleText(page, '.topbar .brand-wordmark') !== null, shown, `${width}x${height} wordmark`);
      await page.close();
    }
    console.log('PASS selected day stays in the top bar');

    // 320×256: every menu item whole, none cut by the bar.
    {
      const page = await open(browser, { width: 320, height: 256 });
      const nav = await page.evaluate(() => {
        const bar = document.querySelector('.topbar > nav');
        return { overflow: bar.scrollWidth - bar.clientWidth, right: bar.getBoundingClientRect().right,
          last: document.querySelector('[data-page="sports"]').getBoundingClientRect().right };
      });
      assert.ok(nav.overflow <= 1, `menu needs ${nav.overflow}px of sideways scrolling`);
      assert.ok(nav.last <= 320, `"Spor" ends at ${nav.last}`);
      await page.close();
      console.log('PASS 320x256: the whole menu is visible');
    }

    // Semantics: the devices list is a list; two regions never share a name.
    {
      const page = await open(browser, { width: 1366, height: 768 });
      await open_page(page, 'system');
      await page.waitForFunction(() => document.querySelectorAll('#devices-list .device-row').length > 0);
      assert.equal(await page.locator('#devices-list').getAttribute('role'), null, 'the list keeps its role');
      assert.equal(await page.locator('#devices-list').getAttribute('tabindex'), null);
      await open_page(page, 'sports');
      await page.waitForFunction(() => document.querySelector('#workout-timeline'));
      const names = await page.evaluate(() => {
        const name = el => el.getAttribute('aria-label') || document.getElementById(el.getAttribute('aria-labelledby'))?.textContent || '';
        return ['.workouts-panel', '#workout-timeline'].map(selector => name(document.querySelector(selector)));
      });
      assert.notEqual(names[0], names[1], `same landmark name: ${names}`);
      await page.close();
      console.log('PASS devices stay a list; the workout regions have different names');
    }

    // Tab order: the sections that never scroll on their own are not stops.
    {
      const page = await open(browser, { width: 1366, height: 768 });
      for (const [view, selector] of [['measurements', '#measurements'], ['system', '#system'], ['sports', '#sports']]) {
        await open_page(page, view);
        await page.waitForFunction(id => !document.getElementById(id).hidden, view);
        assert.equal(await page.locator(selector).getAttribute('tabindex'), null, `${selector} is no Tab stop`);
      }
      assert.equal(await page.locator('.workouts-panel').getAttribute('tabindex'), null, '.workouts-panel is no Tab stop');
      await page.close();
      console.log('PASS no Tab stop on a section that does not scroll');
    }

    // The dialog's Save is on screen on a phone in landscape and on the smallest screen.
    for (const [width, height] of [[844, 390], [320, 256], [1366, 768]]) {
      const page = await open(browser, { width, height });
      await page.evaluate(() => document.getElementById('add-measurement').click());
      await page.locator('#measurement-dialog').waitFor();
      const box = await page.locator('#measurement-save').boundingBox();
      assert.ok(box.y >= 0 && box.y + box.height <= height, `${width}x${height}: Kaydet ${box.y}-${box.y + box.height}`);
      assert.equal(await page.evaluate(() => {
        const save = document.getElementById('measurement-save').getBoundingClientRect();
        return document.elementFromPoint(save.left + save.width / 2, save.top + save.height / 2)?.id;
      }), 'measurement-save');
      await page.close();
    }
    console.log('PASS Kaydet stays on screen in the measurement dialog');
  } finally { await browser.close(); }
});
