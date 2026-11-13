// Synthetic archive: recent data exists, but August is empty. Only today's week can say
// the archive is empty; selected weeks/months keep their own empty cells and date headings.
const assert = require('node:assert/strict');
const fx = require('../../frontend/preview/fixtures.cjs');
const { BASE, launch, run } = require('./lib.cjs');
const { contained } = require('./helpers/accessibility.cjs');
const blank = fx.scenario('empty');

run(async () => {
  const browser = await launch();
  try {
    for (const viewport of [{ width: 1280, height: 720 }, { width: 390, height: 844 }]) {
      for (const scenario of ['week', 'month', 'archive']) {
        const archiveEmpty = scenario === 'archive';
        const page = await browser.newPage({ viewport, reducedMotion: 'reduce' });
        const errors = []; page.on('pageerror', error => errors.push(error.message));
        await page.clock.setFixedTime(new Date(fx.NOW));
        await page.route('**/*', route => {
          const url = new URL(route.request().url()), p = url.pathname;
          if (p === '/health') return route.fulfill({ json: archiveEmpty ? { ...blank.health, configuration: 'healthy' } : fx.health });
          if (p === '/api/dashboard') {
            const empty = archiveEmpty || (url.searchParams.get('end_date') || fx.TODAY) < '2026-10-01';
            return route.fulfill({ json: (empty ? blank.dashboard : fx.dashboard)(url.searchParams) });
          }
          if (p === '/sync/status') return route.fulfill({ json: [] });
          if (/^\/(api|sync|auth|google-health)\//.test(p)) return route.fulfill({ status: 404, json: {} });
          return route.continue();
        });
        await page.routeWebSocket(/\/api\/codex\/chat$/, () => {});
        await page.goto(BASE);
        await page.waitForFunction(today => document.querySelector(`#history-rows tr[data-key="${today}"]`) &&
          document.getElementById('overview').getAttribute('aria-busy') === 'false', fx.TODAY);
        const status = page.locator('#overview-state'), title = page.locator('#history-title');
        assert.equal(await title.textContent(), 'Son 7 gün');
        assert.equal(await status.isVisible(), archiveEmpty);
        await contained(page);
        if (scenario === 'week') {
          await page.locator('#header-date').click();
          await page.locator('#calendar-prev').click();
          await page.locator('#calendar-prev').click();
          await page.locator('[data-date="2026-08-15"]').click();
          await page.waitForFunction(() => document.querySelector('#history-rows tr[data-key="2026-08-15"]'));
          assert.equal(await status.isVisible(), false, 'an empty selected week is not an empty archive');
          assert.equal(await title.textContent(), 'Seçilen 7 gün');
          assert.equal(await page.locator('#history-rows > tr').count(), 7);
        } else {
          await page.locator('#history-toggle').click();
          await page.locator('#history-month').fill('2026-08');
          await page.locator('#history-month').dispatchEvent('change');
          await page.waitForFunction(() => document.querySelectorAll('#history-rows > tr').length === 31 &&
            document.querySelector('#history-rows tr[data-key="2026-08-31"]'));
          assert.equal(await status.isVisible(), archiveEmpty, 'a month must preserve the current archive status');
          assert.equal(await title.textContent(), 'Ağustos 2026');
          if (archiveEmpty) {
            assert.equal(await page.locator('#overview-state-title').textContent(), 'Henüz sağlık verisi yok');
            assert.equal(await page.locator('#overview-state-action').textContent(), 'Şimdi eşitle');
          }
        }
        assert.ok((await page.locator('#history-rows > tr > td:nth-child(2)').allTextContents()).every(text => text === '—'));
        await contained(page);
        if (scenario === 'week') {
          await page.locator('#header-date').click();
          await page.locator('#calendar-today').click();
          await page.waitForFunction(() => document.getElementById('history-title').textContent === 'Son 7 gün');
          assert.equal(await status.isVisible(), false);
        }
        assert.deepEqual(errors, []);
        console.log(`PASS ${viewport.width}x${viewport.height}: ${scenario} empty state and heading`);
        await page.close();
      }
    }
  } finally { await browser.close(); }
});
