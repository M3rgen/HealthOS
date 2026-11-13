// Synthetic fixtures; no live account records.
// Sistem › Hakkında: the medical-scope note, where the data lives and what Umay sends, reachable
// inside the Sistem page (which scrolls inside itself) without the main page scrolling.
const assert = require('node:assert/strict');
const fx = require('../../frontend/preview/fixtures.cjs');
const { BASE, launch, run, shot, showSystemGroup } = require('./lib.cjs');
const { contained } = require('./helpers/accessibility.cjs');

run(async () => {
  const browser = await launch();
  try {
    for (const viewport of [{ width: 1280, height: 720 }, { width: 768, height: 1024 }, { width: 375, height: 667 }]) {
      const name = `${viewport.width}x${viewport.height}`;
      const page = await browser.newPage({ viewport, reducedMotion: 'reduce' });
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.route('**/*', route => {
        const url = new URL(route.request().url());
        const p = url.pathname, q = url.searchParams;
        const json = body => route.fulfill({ json: typeof body === 'function' ? body(q) : body });
        if (p === '/health') return json(fx.health);
        if (p === '/api/dashboard') return json(fx.dashboard);
        if (p === '/api/dashboard/history') return json(fx.history);
        if (p === '/api/sync/coverage') return json(fx.coverage);
        if (p === '/api/webhooks/status') return json(fx.webhookStatus);
        if (p === '/api/devices') return json(fx.devices);
        if (p === '/sync/status') return json(fx.syncStatus);
        if (/^\/(api|sync|auth|google-health)\//.test(p)) return route.fulfill({ status: 404, json: {} });
        return route.continue();
      });
      await page.routeWebSocket(/\/api\/codex\/chat$/, () => {});
      await page.goto(BASE);
      await page.locator('[data-page="system"]').click();
      await showSystemGroup(page, 'settings');
      const about = page.locator('#system .about-panel');
      await about.waitFor();
      assert.equal(await page.getByRole('region', { name: 'Sistem' }).getByRole('heading', { name: 'Hakkında' }).count(), 1);
      const text = (await about.innerText()).replace(/\s+/g, ' ');
      assert.match(text, /HealthOS tıbbi cihaz değildir; teşhis veya tedavi için kullanılmaz\./);
      assert.match(text, /HRV ve SpO₂ değerleri cihaz tahminidir/);
      assert.match(text, /Veri yalnız senin sunucunda kalır/);
      assert.match(text, /Umay açıksa OpenAI’ye gidenler/);
      assert.doesNotMatch(text, /codex/i, 'the bridge is never named to users');
      // The panel sits inside the Sistem page's own scroll area; scrolling there reaches it.
      await about.scrollIntoViewIfNeeded();
      const box = await about.boundingBox();
      assert.ok(box.y >= 0 && box.y < viewport.height, `about panel reachable: ${JSON.stringify(box)}`);
      assert.ok(await page.locator('#system').evaluate(el => el.scrollTop > 0 || el.scrollHeight <= el.clientHeight));
      await contained(page);
      await page.screenshot({ path: shot(`about-${name}.png`) });
      assert.deepEqual(errors, []);
      console.log(`PASS ${name}: Hakkında shows the medical scope, data location and Umay transfer note; no page scroll`);
      await page.close();
    }
  } finally { await browser.close(); }
});
