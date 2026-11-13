// Synthetic fixtures; no live account records.
// A failed /health must not read as a statement about the archive: "Henüz veri kaydedilmedi" is
// only for a report that says so (latest_health_write: null).
const assert = require('node:assert/strict');
const fx = require('../../frontend/preview/fixtures.cjs');
const { BASE, launch, run, showCard, until } = require('./lib.cjs');

const NO_WRITE = /Henüz veri kaydedilmedi/;

async function open(browser, health, dashboard = true) {
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  await page.route('**/*', route => {
    const url = new URL(route.request().url());
    if (url.pathname === '/health') return route.fulfill(health);
    if (url.pathname === '/api/dashboard') return dashboard
      ? route.fulfill({ json: fx.dashboard(url.searchParams) })
      : route.fulfill({ status: 500, json: { detail: 'Internal Server Error' } });
    if (url.pathname === '/sync/status') return route.fulfill({ json: fx.syncStatus });
    if (/^\/(api|sync|auth|google-health)\//.test(url.pathname)) return route.fulfill({ status: 404, json: {} });
    return route.continue();
  });
  await page.goto(BASE);
  await showCard(page, 'connection');
  await until(async () => (await page.locator('#service-status').textContent()) !== 'Kontrol ediliyor' &&
    (await page.locator('#webhook-info').textContent()).trim() !== '', 'the connection panel is painted');
  return page;
}
const panel = page => page.evaluate(() => ({
  service: document.getElementById('service-status').textContent,
  write: document.getElementById('webhook-info').textContent,
}));

run(async () => {
  const browser = await launch();
  try {
    let page = await open(browser, { status: 500, json: { detail: 'Internal Server Error' } });
    await until(async () => (await panel(page)).service === 'Servise ulaşılamıyor', 'a 500 reads as unreachable');
    assert.doesNotMatch((await panel(page)).write, NO_WRITE, '500 + JSON must not claim an empty archive');
    await page.close();

    // The documented 503 report: the service is known to be down; the archive is unknown.
    page = await open(browser, { status: 503, json: { status: 'failed', database: 'unavailable', scheduler: 'running', configuration: 'healthy', service: 'healthos' } });
    await until(async () => (await panel(page)).service === 'Servis hatası', 'the 503 report is read');
    assert.doesNotMatch((await panel(page)).write, NO_WRITE, 'a report without the field says nothing about the archive');
    await page.close();

    // Control: a healthy report that really has no write still says so.
    page = await open(browser, { json: { ...fx.health, latest_health_write: null } });
    await until(async () => NO_WRITE.test((await panel(page)).write), 'an explicit null is the empty archive');
    await page.close();

    // /api/dashboard failing is the error state, never the empty state.
    page = await open(browser, { json: fx.health }, false);
    await page.waitForFunction(() => document.querySelector('#overview-state').dataset.state === 'error');
    await page.close();
    console.log('PASS: failed /health is distinct from an empty archive');
  } finally {
    await browser.close();
  }
});
