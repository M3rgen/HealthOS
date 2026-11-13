// Source precedence (maintainer, 7 Nov 2026): Garmin first for sport activities, the Fitbit Air
// first for every other metric. Precedence selects what is shown first; the other sources stay
// visible as text. Checks the day table badges and "Diğer kaynaklar" sub-row, the folded
// secondary workout, the device ranks and page containment at four viewport sizes. Synthetic
// fixtures only; no live account records.
const assert = require('node:assert/strict');
const path = require('node:path');
const { BASE, launch, run, shot, showSystemGroup } = require('./lib.cjs');
const fx = require(path.join(__dirname, '../../frontend/preview/fixtures.cjs'));

const sizes = [{width:1440,height:900}, {width:1280,height:720},
  {width:390,height:844}, {width:320,height:700}];
const contained = async page => assert.deepEqual(await page.evaluate(() => ({
  x:document.documentElement.scrollWidth > innerWidth,
  y:document.documentElement.scrollHeight > innerHeight,
  bodyX:document.body.scrollWidth > innerWidth, bodyY:document.body.scrollHeight > innerHeight,
})), {x:false,y:false,bodyX:false,bodyY:false});
// A capture right after a DOM change can race the next frame (the WebGL body canvas re-reads
// pixels in the headless shell); let two frames settle before every screenshot.
const settle = page => page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));

run(async () => {
  const browser = await launch();
  try {
    for (const viewport of sizes) {
      const context = await browser.newContext({viewport, timezoneId:'America/New_York'});
      await context.addInitScript(() => localStorage.setItem('healthos.timezone.v2', 'Europe/Brussels'));
      const page = await context.newPage();
      await page.clock.setFixedTime(new Date(fx.NOW));
      const errors = []; page.on('pageerror', error => errors.push(error.message));
      await page.route('**/health', route => route.fulfill({json:fx.health}));
      await page.route('**/api/dashboard?**', route => route.fulfill({json:fx.dashboardWithSources(new URL(route.request().url()).searchParams)}));
      await page.route('**/sync/status', route => route.fulfill({json:fx.syncStatus}));
      await page.route('**/api/sync/coverage?**', route => route.fulfill({json:fx.coverage(new URL(route.request().url()).searchParams)}));
      await page.route('**/api/webhooks/status', route => route.fulfill({json:fx.webhookStatus}));
      await page.route('**/api/devices', route => route.fulfill({json:fx.devices}));
      await page.route('**/api/dashboard/workouts**', route => route.fulfill({json:fx.workouts(new URL(route.request().url()).searchParams)}));
      await page.goto(BASE);

      // Day table: seven data rows; two of them carry Garmin alternatives for steps and sleep.
      await page.waitForFunction(() => document.querySelectorAll('#history-rows tr:not(.source-alternatives)').length === 7);
      assert.equal(await page.locator('#history-rows tr').count(), 9);
      const badges = page.locator('#history-rows .source-badge');
      assert.deepEqual(await badges.allTextContents(), ['Fitbit', 'Fitbit', 'Fitbit', 'Fitbit']);
      const toggles = page.locator('#history-rows .source-toggle');
      assert.equal(await toggles.count(), 2);
      assert.deepEqual(await toggles.allTextContents(), ['Diğer kaynaklar', 'Diğer kaynaklar']);
      assert.deepEqual(await toggles.evaluateAll(els => els.map(el => el.getAttribute('aria-expanded'))), ['false', 'false']);
      assert.equal(await page.locator('#history-rows .source-alternatives:not([hidden])').count(), 0);
      // Rows without a second source stay as before: no badge, no toggle, seven cells.
      assert.equal(await page.locator('#history-rows tr:not(.source-alternatives):not(:has(.source-toggle))').count(), 5);
      assert.deepEqual(await page.locator('#history-rows tr:not(.source-alternatives)').evaluateAll(rows => [...new Set(rows.map(row => row.children.length))]), [7]);
      await contained(page);

      // Keyboard: Enter on the toggle reveals the alternatives as text, Enter again folds them.
      const toggle = toggles.first();
      await toggle.focus(); await page.keyboard.press('Enter');
      assert.equal(await toggle.getAttribute('aria-expanded'), 'true');
      const sub = page.locator(`#${await toggle.getAttribute('aria-controls')}`);
      assert.equal(await sub.isHidden(), false);
      assert.equal(await sub.evaluate(el => el.classList.contains('source-alternatives') && el.previousElementSibling.contains(document.activeElement)), true);
      const text = await sub.textContent();
      assert.match(text, /Adım: Garmin [\d.]+ · Uyku: Garmin \d+ sa \d+ dk/);
      assert.match(text, /Garmin/);
      assert.equal(await page.locator('#history-rows .source-alternatives:not([hidden])').count(), 1);
      await contained(page);
      await settle(page);
      await page.screenshot({path:shot(`source-precedence-${viewport.width}.png`)});
      await page.keyboard.press('Enter');
      assert.equal(await toggle.getAttribute('aria-expanded'), 'false');
      assert.equal(await sub.isHidden(), true);
      await contained(page);

      // Sports: the Garmin run is the top-level entry; the overlapping Fitbit walk folds under it.
      await page.locator('[data-page="sports"]').click();
      await page.waitForFunction(() => document.querySelector('#workout-timeline').getAttribute('aria-busy') === 'false');
      assert.equal(await page.locator('.workout-entry').count(), 12);
      assert.equal(await page.locator('.workout-entry[data-workout-id="w7-fitbit"]').count(), 0);
      const garmin = page.locator('#workout-rows > li > ol > .workout-entry[data-workout-id="w7"]');
      assert.equal(await garmin.count(), 1);
      assert.equal(await garmin.getAttribute('data-source'), 'Garmin');
      assert.equal(await garmin.locator('> article > .workout-entry-heading .workout-source').textContent(), 'Garmin');
      assert.equal(await garmin.locator('> article > .workout-entry-heading .workout-priority').textContent(), 'Öncelikli');
      const details = garmin.locator('details.workout-alternatives');
      assert.equal(await details.count(), 1);
      assert.equal(await details.locator('summary').textContent(), 'Diğer kaynaklar (1)');
      assert.equal(await details.evaluate(el => el.open), false);
      const folded = details.locator('.workout-alternative[data-workout-id="w7-fitbit"]');
      assert.equal(await folded.count(), 1);
      assert.equal(await folded.locator('.workout-source').textContent(), 'Fitbit');
      assert.equal(await folded.locator('.workout-priority').textContent(), 'İkincil');
      assert.equal(await page.locator('.workout-priority', {hasText:'Öncelikli'}).count(), 1);
      assert.doesNotMatch(await page.locator('#workout-rows').textContent(), /Google · |Codex/);
      assert.match(await page.locator('#workout-status').textContent(), /^12 antrenman · 1 ikincil kayıt$/);
      assert.match(await page.locator('#sports .section-title p').textContent(), /Cihazlardan gelen ve elle girilen antrenmanlar/);
      await contained(page);
      await details.locator('summary').focus(); await page.keyboard.press('Enter');
      assert.equal(await details.evaluate(el => el.open), true);
      assert.equal(await folded.isVisible(), true);
      assert.match(await folded.locator('.workout-time').textContent(), /\d\d:\d\d – \d\d:\d\d/);
      assert.deepEqual(await folded.locator('dt').allTextContents(), ['Süre', 'Kalori', 'Mesafe']);
      await contained(page);

      // Devices: each row names its rank in words; the Garmin watch shows its last pull.
      await page.getByRole('button', {name:'Sistem', exact:true}).click();
      await page.waitForFunction(() => document.querySelectorAll('#devices-list .device-row').length === 4);
      await showSystemGroup(page, 'devices');
      const rows = page.locator('#devices-list .device-row');
      assert.deepEqual(await rows.locator('.device-precedence').allTextContents(), [
        'Aktivite: ikincil · Sağlık: öncelikli', 'Aktivite: ikincil · Sağlık: ikincil',
        'Aktivite: sırasız · Sağlık: sırasız', 'Aktivite: öncelikli · Sağlık: ikincil']);
      const watch = rows.nth(3);
      assert.equal(await watch.locator('.device-name').textContent(), 'Garmin');
      assert.match(await watch.locator('.device-kind').textContent(), /Garmin · Saat/);
      assert.equal(await watch.locator('.dot').textContent(), 'Bağlı');
      assert.match(await watch.locator('.device-meta').textContent(), /Son çekim: 30 dk önce/);
      assert.doesNotMatch(await watch.locator('.device-meta').textContent(), /Google son eşitleme/);
      await contained(page);
      const open = page.getByRole('button', {name:'Garmin ayrıntıları', exact:true});
      await open.focus(); await page.keyboard.press('Enter');
      assert.equal(await page.locator('#devices-dialog').evaluate(el => el.open), true);
      const dialog = await page.locator('#devices-details').textContent();
      assert.match(dialog, /Öncelik\s*Aktivite: öncelikli · Sağlık: ikincil/);
      assert.match(dialog, /Son çekim\s*16 Ekim 2026 16:00 · 30 dk önce/);
      assert.match(dialog, /Platform\s*Garmin/);
      assert.match(dialog, /Aktiviteler.*Günlük özetler.*Nabız.*Uyku.*Adım/);
      await contained(page);
      await page.keyboard.press('Escape');
      assert.equal(await open.evaluate(el => el === document.activeElement), true);
      await settle(page);
      await page.screenshot({path:shot(`source-precedence-devices-${viewport.width}.png`)});
      assert.deepEqual(errors, []);
      console.log(`PASS ${viewport.width}x${viewport.height}: table badges and sub-row, folded secondary workout, device ranks, containment`);
      await context.close();
    }
  } finally { await browser.close(); }
});
