// Synthetic fixtures only. All text nodes, four pages, every matrix size, both languages.
const assert = require('node:assert/strict');
const fx = require('../../frontend/preview/fixtures.cjs');
const fs = require('node:fs');
const path = require('node:path');
const { BASE, OUTPUT, launch, run } = require('./lib.cjs');
const { auditText } = require('./helpers/text-audit.cjs');
const { sizes } = require('./viewport-floors.json');
// Include the font-preference viewports used by viewport-matrix as well as its floor matrix.
const fontSizes = [[1920, 1080], [1366, 768], [1280, 720], [1024, 600], [844, 390], [390, 844], [375, 667], [320, 568], [320, 256]];
const cases = [...sizes.map(([w, h]) => [w, h, 16]), ...fontSizes.flatMap(([w, h]) => [24, 32].map(font => [w, h, font]))];

run(async () => {
  const browser = await launch();
  const failures = [];
  try {
    for (const [width, height, font] of cases) for (const language of ['tr', 'en']) {
      const page = await browser.newPage({ viewport: { width, height }, reducedMotion: 'reduce', timezoneId: 'UTC' });
      if (font !== 16) {
        const cdp = await page.context().newCDPSession(page);
        await cdp.send('Page.setFontSizes', { fontSizes: { standard: font, fixed: Math.round(font * 0.8125) } });
      }
      await page.addInitScript(lang => localStorage.setItem('healthos.language', lang), language);
      await page.clock.setFixedTime(new Date(fx.NOW));
      await page.route('**/*', route => {
        const url = new URL(route.request().url()), p = url.pathname;
        if (url.origin !== new URL(BASE).origin) return route.abort();
        const routes = { '/health': fx.health, '/api/dashboard': fx.dashboard,
          '/api/dashboard/history': fx.history, '/api/dashboard/measurements': fx.measurements,
          '/api/dashboard/workouts': fx.workouts, '/api/sync/coverage': fx.coverage,
          '/api/webhooks/status': fx.webhookStatus, '/api/devices': fx.devices,
          '/sync/status': fx.syncStatus, '/api/settings/appearance': fx.appearanceSettings,
          '/api/settings/display': { schema_version: 1, preferences: { timezone: 'UTC', motion: false }, updated_at: fx.NOW } };
        if (p in routes) return route.fulfill({ json: typeof routes[p] === 'function' ? routes[p](url.searchParams) : routes[p] });
        if (/^\/(api|sync|auth|google-health)\//.test(p)) return route.fulfill({ status: 404, json: {} });
        return route.continue();
      });
      await page.routeWebSocket(/\/api\/codex\/chat$/, () => {});
      await page.goto(BASE);
      await page.waitForFunction(() => document.querySelectorAll('#history-rows tr').length === 7);
      await page.evaluate(() => document.fonts.ready);
      for (const view of ['overview', 'measurements', 'sports', 'system']) {
        await page.evaluate(id => document.querySelector(`[data-page="${id}"]`).click(), view);
        const ready = { overview: '#history-rows tr', measurements: '#measurement-rows tr', sports: '#workout-rows .workout-day', system: '#sync-rows tr' };
        await page.locator(ready[view]).first().waitFor({ state: 'attached' });
        // Audit every tab too: a hidden panel must not evade coverage on small viewports.
        const tabs = view === 'overview' ? '.overview-tabs .panel-tab' : view === 'system' ? '.system-tabs .panel-tab' : null;
        const states = tabs && await page.locator(tabs).first().isVisible() ? await page.locator(tabs).count() : 1;
        for (let tab = 0; tab < states; tab++) {
          if (states > 1) await page.locator(tabs).nth(tab).click();
          await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
          const result = await page.evaluate(auditText);
          const label = `${width}x${height} font=${font} ${language} ${view}/${tab}`;
          if (result.overflow.length || result.overlap.length) failures.push({ label, ...result });
        }
      }
      await page.close();
      console.log(`INFO scanned ${width}x${height} font=${font} ${language}`);
    }
  } finally { await browser.close(); }
  fs.mkdirSync(OUTPUT, { recursive: true });
  fs.writeFileSync(path.join(OUTPUT, 'text-overflow.json'), JSON.stringify(failures, null, 2));
  assert.equal(failures.length, 0, JSON.stringify(failures.slice(0, 12), null, 2));
  console.log('PASS all visible text fits without overlap');
});
