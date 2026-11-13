// Synthetic fixtures; no live account records.
// Notices: a failed dashboard request shows an error that the next successful poll removes;
// success notices leave on their own after 5 s; every notice can be closed; a long notice stays
// small and scrolls inside itself, so the page never scrolls.
const assert = require('node:assert/strict');
const fx = require('../../frontend/preview/fixtures.cjs');
const { BASE, launch, run, shot, until, showCard } = require('./lib.cjs');
const { contained } = require('./helpers/accessibility.cjs');

async function setup(browser, viewport) {
  const page = await browser.newPage({ viewport });
  const state = { failDashboard: true, dashboards: 0, failSync: false };
  await page.addInitScript(() => {
    const set = window.setInterval;
    window.setInterval = (callback, delay, ...args) => {
      if (delay === 60000) window.pollNow = callback;
      return set(callback, delay, ...args);
    };
  });
  await page.route('**/*', route => {
    const url = new URL(route.request().url());
    if (url.pathname === '/health') return route.fulfill({ json: fx.health });
    if (url.pathname === '/api/dashboard') {
      state.dashboards++;
      return state.failDashboard ? route.fulfill({ status: 503, json: { detail: 'Sentetik hata' } }) : route.fulfill({ json: fx.dashboard(url.searchParams) });
    }
    if (url.pathname === '/sync/all') return route.fulfill(state.failSync
      ? { status: 500, json: { detail: `Google bağlantısı kurulamadı. ${'Ayrıntı yeniden denenecek. '.repeat(30)}` } }
      : { status: 202, json: { status: 'accepted', data_types: ['steps'], sequence: 1 } });
    if (url.pathname === '/sync/status') return route.fulfill({ json: [{ data_type: 'steps', status: 'completed', last_error: null, records_downloaded: 1, last_run: { sequence: 2, inserted: 3, updated: 0, failed: false } }] });
    if (/^\/(api|sync|auth|google-health)\//.test(url.pathname)) return route.fulfill({ status: 404, json: {} });
    return route.continue();
  });
  await page.goto(BASE);
  return { page, state };
}

run(async () => {
  const browser = await launch();
  try {
    for (const viewport of [{ width: 1280, height: 720 }, { width: 390, height: 844 }, { width: 320, height: 568 }]) {
      const name = `${viewport.width}x${viewport.height}`;
      const { page, state } = await setup(browser, viewport);
      const notice = page.locator('#notice');
      await notice.waitFor({ state: 'visible' });
      assert.match(await page.locator('#notice-text').textContent(), /Veriler alınamadı/);
      assert.equal(await notice.evaluate(el => el.classList.contains('error')), true);
      // A2: the status text is read as it is; no label replaces it.
      assert.equal(await page.locator('#notice-text').getAttribute('role'), 'status');
      assert.equal(await page.locator('[aria-label="Durum mesajı"]').count(), 0);
      assert.equal(await notice.getAttribute('aria-label'), null);
      await page.screenshot({ path: shot(`notice-error-${name}.png`) });
      await contained(page);

      // F2: the next successful poll removes the error from that request.
      state.failDashboard = false;
      await page.waitForFunction(() => typeof window.pollNow === 'function');
      await page.evaluate(() => window.pollNow());
      await notice.waitFor({ state: 'hidden' });
      await page.waitForFunction(() => document.querySelectorAll('#history-rows tr').length === 7);

      // A successful sync reports its result, then the notice leaves on its own after 5 s.
      await showCard(page, 'connection');
      await page.locator('#sync-button').click();
      await notice.waitFor({ state: 'visible' });
      await page.waitForFunction(() => document.querySelector('#notice-text').textContent.startsWith('Eşitleme tamamlandı'));
      const shownAt = Date.now();
      await notice.waitFor({ state: 'hidden', timeout: 15000 });
      const lasted = Date.now() - shownAt;
      assert.ok(lasted > 3500, `success notice left after ${lasted} ms`);

      // A long error stays small, scrolls inside itself and closes with its button.
      state.failSync = true;
      await page.locator('#sync-button').click();
      await notice.waitFor({ state: 'visible' });
      await page.waitForFunction(() => document.querySelector('#notice-text').textContent.includes('Google bağlantısı kurulamadı'));
      const box = await page.evaluate(() => {
        const notice = document.getElementById('notice'), text = document.getElementById('notice-text');
        const close = document.getElementById('notice-close').getBoundingClientRect(), rect = notice.getBoundingClientRect();
        return { height: rect.height, scrolls: text.scrollHeight > text.clientHeight, fontSize: parseFloat(getComputedStyle(text).fontSize),
          closeVisible: close.width >= 24 && close.height >= 24 && close.top >= rect.top && close.bottom <= rect.bottom + 1 && close.right <= innerWidth };
      });
      assert.ok(box.height <= Math.max(6 * box.fontSize + 2, 0.16 * viewport.height + 2), JSON.stringify(box));
      assert.ok(box.scrolls, 'a long notice scrolls inside its text');
      assert.ok(box.closeVisible, JSON.stringify(box));
      await contained(page);
      await page.screenshot({ path: shot(`notice-long-${name}.png`) });
      await page.waitForTimeout(5500);
      assert.equal(await notice.isVisible(), true, 'an error does not time out');
      await page.locator('#notice-close').focus();
      await page.keyboard.press('Enter');
      await notice.waitFor({ state: 'hidden' });
      assert.equal(await page.evaluate(() => document.activeElement.matches('[data-page][aria-current="page"]')), true,
        'focus moves to the current page button, not to the document');
      await contained(page);
      await until(async () => !(await notice.isVisible()), 'closed notice stays closed');
      console.log(`PASS ${name}: failed request then successful poll clears its error, success leaves after ${lasted} ms, long error scrolls inside and closes, no page scroll`);
      await page.close();
    }
  } finally {
    await browser.close();
  }
});
