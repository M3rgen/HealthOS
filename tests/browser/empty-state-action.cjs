// Synthetic fixtures. The empty overview offers what this installation can actually do next:
// connect Google, sync, or add a measurement by hand where no source can be connected from here.
const assert = require('node:assert/strict');
const fx = require('../../frontend/preview/fixtures.cjs');
const { BASE, launch, run } = require('./lib.cjs');

const blank = fx.scenario('empty');

async function open(browser, health) {
  const page = await browser.newPage({ viewport: { width: 1366, height: 768 }, reducedMotion: 'reduce' });
  await page.clock.setFixedTime(new Date(fx.NOW));
  await page.route('**/*', route => {
    const url = new URL(route.request().url()), p = url.pathname;
    const json = body => route.fulfill({ json: typeof body === 'function' ? body(url.searchParams) : body });
    if (p === '/health') return json(health);
    if (p === '/api/dashboard') return json(blank.dashboard);
    if (p === '/api/dashboard/measurements') return json([]);
    if (p === '/api/devices') return json(blank.devices);
    if (p === '/sync/status') return json([]);
    if (p === '/auth/google/start') return route.fulfill({ contentType: 'text/html', body: '<!doctype html><title>google</title>' });
    if (/^\/(api|sync|auth|google-health)\//.test(p)) return route.fulfill({ status: 404, json: {} });
    return route.continue();
  });
  await page.routeWebSocket(/\/api\/codex\/chat$/, () => {});
  await page.goto(BASE);
  await page.waitForFunction(() => document.querySelector('#overview-state').dataset.state === 'empty' &&
    document.getElementById('service-status').textContent !== 'Kontrol ediliyor');
  return page;
}
const action = page => page.locator('#overview-state-action');
const detail = page => page.locator('#overview-state-detail').textContent();

run(async () => {
  const browser = await launch();
  try {
    // Google selected but not connected: the button really goes to the connection.
    {
      const page = await open(browser, blank.health);
      assert.equal(await action(page).textContent(), 'Google’a bağlan');
      await action(page).click();
      await page.waitForURL('**/auth/google/start');
      await page.close();
    }
    // Google connected, no data yet: the next step is a sync.
    {
      const page = await open(browser, { ...blank.health, configuration: 'healthy' });
      assert.equal(await action(page).textContent(), 'Şimdi eşitle');
      await page.close();
    }
    // No source on this installation: nothing here can connect one, so it says where that happens
    // and offers what works; Sistem shows no Google panels.
    for (const [sources, words] of [[[], /kurulum belgesi/], [['garmin'], /Garmin seçili/]]) {
      const page = await open(browser, { ...blank.health, active_sources: sources });
      assert.equal(await action(page).textContent(), 'Ölçüm ekle');
      assert.match(await detail(page), words);
      await action(page).click();
      await page.locator('#measurement-dialog').waitFor();
      await page.locator('#measurement-close').click();
      await page.locator('[data-page="system"]').click();
      await page.waitForFunction(() => !document.getElementById('system').hidden);
      assert.equal(await page.locator('.coverage-panel').isVisible(), false);
      assert.equal(await page.locator('.webhook-status-panel').isVisible(), false);
      await page.close();
    }
    console.log('PASS the empty overview offers a step that works on this installation');
  } finally { await browser.close(); }
});
