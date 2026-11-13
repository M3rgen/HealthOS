// Synthetic fixtures. A bad or missing response must not freeze the dashboard: loading texts
// resolve, a retry control appears, and polling keeps running.
const assert = require('node:assert/strict');
const fx = require('../../frontend/preview/fixtures.cjs');
const { BASE, launch, run, shot, until } = require('./lib.cjs');
const { contained } = require('./helpers/accessibility.cjs');

async function open(browser, mode) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 }, timezoneId: 'UTC' });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  const state = { mode, dashboards: 0, health: 0, lastDashboard: null };
  await page.clock.install({ time: new Date(fx.NOW) });
  await page.route('**/*', route => {
    const url = new URL(route.request().url());
    const p = url.pathname;
    if (p === '/health') {
      state.health++;
      return state.mode === 'offline' ? route.abort() : route.fulfill({ json: fx.health });
    }
    if (p === '/api/dashboard') {
      state.dashboards++;
      if (state.mode === 'offline') return route.abort();
      if (state.mode === 'empty') return route.fulfill({ json: { days: [] } });
      return route.fulfill({ json: fx.dashboard(url.searchParams) });
    }
    if (p === '/sync/status') return state.mode === 'offline' ? route.abort() : route.fulfill({ json: fx.syncStatus });
    if (/^\/(api|sync|auth|google-health)\//.test(p)) return state.mode === 'offline' ? route.abort() : route.fulfill({ status: 404, json: {} });
    return route.continue();
  });
  await page.routeWebSocket(/\/api\/codex\/chat$/, () => {});
  await page.goto(BASE);
  return { page, context, errors, state };
}
const notice = page => page.locator('#notice-text').textContent();
const retry = page => page.locator('#notice .notice-action');
const settled = page => page.waitForFunction(() => document.getElementById('range-caption').textContent !== 'Yükleniyor' && !document.getElementById('notice').hidden);

run(async () => {
  const browser = await launch();
  try {
    // A malformed response (days: []): no exception, caption resolved, retry works, polling continues.
    {
      const { page, context, errors, state } = await open(browser, 'empty');
      await settled(page);
      await page.screenshot({ path: shot('resilience-retry.png') });
      assert.match(await notice(page), /Veriler alınamadı/);
      assert.equal(await page.locator('#range-caption').textContent(), 'Veri alınamadı');
      assert.equal(await page.locator('#steps-note').textContent(), 'Veri alınamadı');
      assert.equal(await retry(page).textContent(), 'Yeniden dene');
      for (const viewport of [{ width: 390, height: 844 }, { width: 320, height: 256 }, { width: 1280, height: 800 }]) {
        await page.setViewportSize(viewport);
        // The tab bar follows the media query one task later; measure the settled layout.
        await page.waitForFunction(() => matchMedia('(max-width: 56.25em), (max-height: 30em)').matches ===
          document.querySelector('.overview-grid').hasAttribute('data-tabbed'));
        const look = await retry(page).evaluate(button => {
          const box = button.getBoundingClientRect(), style = getComputedStyle(button), holder = button.parentElement.getBoundingClientRect();
          return { box: [box.width, box.height], inside: box.left >= holder.left && box.right <= holder.right && box.top >= holder.top && box.bottom <= holder.bottom,
            onScreen: box.top >= 0 && box.bottom <= innerHeight && box.right <= innerWidth, visible: style.visibility === 'visible' && style.display !== 'none',
            border: style.borderTopStyle !== 'none' && parseFloat(style.borderTopWidth) > 0, padded: parseFloat(style.paddingLeft) >= 8, cursor: style.cursor };
        });
        assert.equal(look.visible && look.border && look.padded && look.cursor === 'pointer', true, `retry looks like a button at ${viewport.width}×${viewport.height}: ${JSON.stringify(look)}`);
        assert.equal(look.inside && look.onScreen, true, `retry fits the notice at ${viewport.width}×${viewport.height}: ${JSON.stringify(look)}`);
        if (viewport.width <= 540) assert.ok(look.box[1] >= 44, `retry is a phone target at ${viewport.width}×${viewport.height}: ${JSON.stringify(look)}`);
        await contained(page);
      }
      const before = state.dashboards;
      await page.clock.fastForward(61000);
      await until(() => state.dashboards > before, 'polling must keep running after a malformed response');
      state.mode = 'ok';
      await retry(page).click();
      await page.waitForFunction(() => document.querySelectorAll('#history-rows tr').length === 7);
      assert.equal(await page.locator('#notice').isHidden(), true, 'a successful retry clears the notice');
      assert.match(await page.locator('#range-caption').textContent(), /Ekim 2026$/);
      await contained(page);
      assert.deepEqual(errors, []);
      console.log('PASS malformed response resolves the captions, retries and keeps polling');
      await context.close();
    }
    // Offline from the start: nothing stays in a loading state.
    {
      const { page, context, errors, state } = await open(browser, 'offline');
      await settled(page);
      assert.equal(await page.locator('#service-status').textContent(), 'Servise ulaşılamıyor');
      assert.match(await page.locator('#webhook-info').textContent(), /öğrenilemedi/);
      const polled = state.health;
      await page.locator('[data-page="system"]').click();
      await page.waitForFunction(() => document.getElementById('coverage-summary').textContent.includes('ulaşılamıyor'));
      for (const id of ['coverage-summary', 'webhook-status-summary', 'devices-summary'])
        assert.match(await page.locator(`#${id}`).textContent(), /ulaşılamıyor/, id);
      assert.match(await page.locator('#sync-rows').textContent(), /ulaşılamıyor/);
      await page.locator('[data-page="measurements"]').click();
      await page.waitForFunction(() => document.querySelector('#measurement-rows td')?.textContent.includes('alınamadı'));
      assert.equal(await retry(page).isVisible(), true, 'measurements offer a retry too');
      await page.clock.fastForward(61000);
      await until(() => state.health > polled, 'polling must continue while offline');
      assert.deepEqual(errors, []);
      console.log('PASS offline: System panels and the measurement list resolve, polling continues');
      await context.close();
    }
  } finally { await browser.close(); }
});
