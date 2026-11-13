// R25: real Chromium site-data blocking, synthetic responses only.
const assert = require('node:assert/strict');
const { mkdtempSync, mkdirSync, writeFileSync, rmSync } = require('node:fs');
const { tmpdir } = require('node:os');
const { join } = require('node:path');
const { BASE, chromium, run, STEP_TIMEOUT } = require('./lib.cjs');
const { mock, contained } = require('./helpers/accessibility.cjs');
const fx = require('../../frontend/preview/fixtures.cjs');

run(async () => {
  const profile = mkdtempSync(join(tmpdir(), 'healthos-blocked-storage-'));
  let context;
  try {
    mkdirSync(join(profile, 'Default'));
    writeFileSync(join(profile, 'Default', 'Preferences'), JSON.stringify({
      profile: { default_content_setting_values: { cookies: 2 } },
    }));
    // Playwright's default headless browser (chromium-headless-shell) ignores the profile's
    // content settings, so site data would not be blocked there. Full Chromium in new headless
    // mode reads Default/Preferences and makes the localStorage getter throw SecurityError.
    context = await chromium.launchPersistentContext(profile, { channel: 'chromium', headless: true, locale: 'tr-TR', reducedMotion: 'reduce' });
    context.setDefaultTimeout(STEP_TIMEOUT);
    for (const viewport of [{ width: 1366, height: 768 }, { width: 390, height: 844 }]) {
      const page = await context.newPage();
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.setViewportSize(viewport);
      await page.clock.setFixedTime(new Date(fx.NOW));
      await mock(page);
      await page.goto(BASE);
      assert.equal(await page.evaluate(() => {
        try { void window.localStorage; return null; } catch (error) { return error.name; }
      }), 'SecurityError', 'the browser must actually block the storage getter');
      await page.waitForFunction(() => document.querySelectorAll('#history-rows tr').length === 7);
      await page.waitForFunction(() => document.documentElement.dataset.booted === 'true');
      assert.equal(await page.locator('#boot-failure').count(), 0);
      await contained(page);
      assert.deepEqual(errors, [], `${viewport.width}px: no uncaught page errors`);
      await page.close();
    }
    console.log('PASS blocked site data: seven history rows, no boot failure or page overflow on desktop/mobile');
  } finally {
    try { await context?.close(); } finally { rmSync(profile, { recursive: true, force: true }); }
  }
});
