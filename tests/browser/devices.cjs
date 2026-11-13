// Cihazlarım on the System page: status rows, the details dialog, the empty, Google-unavailable
// and failure states, keyboard access and page containment at four viewport sizes. Synthetic
// fixtures only; no live account records.
const assert = require('node:assert/strict');
const path = require('node:path');
const { BASE, launch, run, until, shot, showSystemGroup } = require('./lib.cjs');
const fx = require(path.join(__dirname, '../../frontend/preview/fixtures.cjs'));

const sizes = [{width:1440,height:900}, {width:1280,height:720},
  {width:390,height:844}, {width:320,height:700}];
const contained = async page => assert.deepEqual(await page.evaluate(() => ({
  x:document.documentElement.scrollWidth > innerWidth,
  y:document.documentElement.scrollHeight > innerHeight,
  bodyX:document.body.scrollWidth > innerWidth, bodyY:document.body.scrollHeight > innerHeight,
})), {x:false,y:false,bodyX:false,bodyY:false});
const inside = async (page, selector, viewport) => {
  const box = await page.locator(selector).boundingBox();
  assert.ok(box && box.x >= 0 && box.y >= 0 && box.x + box.width <= viewport.width + 1 && box.y + box.height <= viewport.height + 1,
    `${selector} leaves the viewport: ${JSON.stringify(box)}`);
};

run(async () => {
  const browser = await launch();
  try {
    for (const viewport of sizes) {
      // Brussels on a New York machine: every shown time must follow the chosen zone.
      const context = await browser.newContext({viewport, timezoneId:'America/New_York'});
      await context.addInitScript(() => localStorage.setItem('healthos.timezone.v2', 'Europe/Brussels'));
      const page = await context.newPage();
      await page.clock.setFixedTime(new Date(fx.NOW));
      const errors = []; page.on('pageerror', error => errors.push(error.message));
      const state = {mode:'ok', requests:0};
      await page.route('**/health', route => route.fulfill({json:fx.health}));
      await page.route('**/api/dashboard?**', route => route.fulfill({json:fx.dashboard(new URL(route.request().url()).searchParams)}));
      await page.route('**/sync/status', route => route.fulfill({json:fx.syncStatus}));
      await page.route('**/api/sync/coverage?**', route => route.fulfill({json:fx.coverage(new URL(route.request().url()).searchParams)}));
      await page.route('**/api/webhooks/status', route => route.fulfill({json:fx.webhookStatus}));
      await page.route('**/api/devices', route => {
        state.requests++;
        if (state.mode === 'fail') return route.fulfill({status:503,json:{detail:'private upstream error'}});
        if (state.mode === 'empty') return route.fulfill({json:{...fx.devices, devices:[]}});
        if (state.mode === 'google-down') return route.fulfill({json:{...fx.devices, google:'unavailable',
          devices:fx.devices.devices.map(({google_last_sync, google_device_type, ...device}) => device)}});
        return route.fulfill({json:fx.devices});
      });
      await page.goto(BASE);
      await page.waitForFunction(() => document.querySelectorAll('#history-rows tr').length === 7);
      assert.equal(state.requests, 0, 'the overview never asks for the device list');
      await page.getByRole('button', {name:'Sistem', exact:true}).click();
      await until(() => state.requests === 1, 'device request starts with the System page');
      await page.waitForFunction(() => document.querySelectorAll('#devices-list .device-row').length === 4);
      await showSystemGroup(page, 'devices');

      // One row per device, the renamed watch merged; status text next to its colour.
      const rows = page.locator('#devices-list .device-row');
      assert.deepEqual(await rows.locator('.device-name').allTextContents(), ['Fitbit Air', 'iPhone', 'Aria Example', 'Garmin']);
      assert.deepEqual(await rows.locator('.dot').allTextContents(), ['Bağlı', 'Gecikmeli', 'Kopuk', 'Bağlı']);
      assert.deepEqual(await rows.locator('.dot').evaluateAll(els => els.map(el => el.className)), ['dot good', 'dot warning', 'dot bad', 'dot good']);
      assert.doesNotMatch(await page.locator('#devices-list').textContent(), /Google Fitbit Air/);
      assert.match(await page.locator('#devices-summary').textContent(), /^4 cihaz · 2 bağlı · 1 gecikmeli · 1 kopuk$/);
      assert.match(await page.locator('#devices-thresholds').textContent(), /48 saat.*7 gün/);
      // Relative age from the frozen clock, exact Brussels time in the tooltip (CEST = UTC+2).
      const watch = rows.nth(0);
      assert.match(await watch.locator('.device-meta').textContent(), /Son veri: 25 dk önce/);
      assert.match(await watch.locator('.device-meta time').first().getAttribute('title'), /16 Ekim 2026 16:05/);
      assert.match(await watch.locator('.device-meta').textContent(), /5 veri türü/);
      assert.match(await watch.locator('.device-meta').textContent(), /Google son eşitleme: 18 dk önce/);
      assert.doesNotMatch(await rows.nth(1).locator('.device-meta').textContent(), /Google son eşitleme/);
      assert.match(await rows.nth(1).locator('.device-meta').textContent(), /Son veri: 2 gün önce/);
      assert.match(await rows.nth(2).locator('.device-meta').textContent(), /Son veri: 1 ay önce/);
      assert.match(await rows.nth(0).locator('.device-kind').textContent(), /Fitbit · Saat/);
      assert.match(await rows.nth(1).locator('.device-kind').textContent(), /Apple Sağlık · Telefon/);
      assert.equal(await page.locator('#devices-google-note').isHidden(), true);
      await contained(page);
      await page.screenshot({path:shot(`devices-${viewport.width}.png`)});

      // Details by keyboard: the old name, every data type and the sightings; focus returns.
      const open = page.getByRole('button', {name:'Fitbit Air ayrıntıları', exact:true});
      await open.focus(); await page.keyboard.press('Enter');
      assert.equal(await page.locator('#devices-dialog').evaluate(el => el.open), true);
      assert.equal(await page.locator('#devices-dialog-title').textContent(), 'Fitbit Air');
      const details = await page.locator('#devices-details').textContent();
      assert.match(details, /Eski adlar.*Google Fitbit Air/);
      assert.match(details, /Günlük dinlenme nabzı.*Nabız.*Oksijen doygunluğu.*Uyku.*Adım/);
      assert.match(details, /İlk görülme.*2 Kasım 2025/);
      assert.match(details, /Son görülme.*16 Ekim 2026 16:15/);
      assert.match(details, /Son veri.*16 Ekim 2026 16:05 · 25 dk önce/);
      assert.match(details, /Google son eşitleme.*16 Ekim 2026 16:12/);
      assert.match(details, /Durum\s*Bağlı/);
      assert.doesNotMatch(details, /A4B5C6D7E8F9|\.json|fingerprint/i);
      await inside(page, '#devices-dialog', viewport);
      await contained(page);
      await page.keyboard.press('Escape');
      assert.equal(await open.evaluate(el => el === document.activeElement), true);
      await page.getByRole('button', {name:'iPhone ayrıntıları', exact:true}).click();
      assert.match(await page.locator('#devices-details').textContent(), /Üretici\s*Example Inc\..*Uygulama\s*com\.example\.health/);
      assert.doesNotMatch(await page.locator('#devices-details').textContent(), /Eski adlar/);
      await page.getByRole('button', {name:'Cihaz ayrıntılarını kapat', exact:true}).click();
      assert.equal(await page.locator('#devices-dialog').evaluate(el => el.open), false);

      // Google unavailable: the archive rows stay, the note appears, no sync line.
      state.mode = 'google-down';
      await page.getByRole('button', {name:'Durumu yenile', exact:true}).click();
      await page.waitForFunction(() => !document.querySelector('#devices-google-note').hidden);
      assert.equal(await rows.count(), 4);
      assert.doesNotMatch(await page.locator('#devices-list').textContent(), /Google son eşitleme/);
      await contained(page);

      // Empty archive.
      state.mode = 'empty';
      await page.getByRole('button', {name:'Durumu yenile', exact:true}).click();
      await page.waitForFunction(() => document.querySelector('#devices-summary').textContent === 'Henüz cihaz görülmedi.');
      assert.equal(await rows.count(), 0);
      await contained(page);

      // Failure: a retry button, no upstream detail, then recovery by keyboard.
      state.mode = 'fail';
      await page.getByRole('button', {name:'Durumu yenile', exact:true}).click();
      await page.waitForFunction(() => document.querySelector('#devices-summary').textContent === 'Cihaz listesi alınamadı.');
      assert.doesNotMatch(await page.locator('.devices-panel').textContent(), /private/);
      assert.equal(await rows.count(), 0);
      state.mode = 'ok';
      const retry = page.getByRole('button', {name:'Cihazları yeniden al', exact:true});
      await retry.focus(); await page.keyboard.press('Enter');
      await page.waitForFunction(() => document.querySelectorAll('#devices-list .device-row').length === 4);
      assert.equal(await retry.isHidden(), true);
      await contained(page);
      assert.deepEqual(errors, []);
      await context.close();
    }
  } finally { await browser.close(); }
});
