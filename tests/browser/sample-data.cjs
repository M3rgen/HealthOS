// Synthetic fixtures; no live account records.
// A fresh install with no working source offers the sample data on the empty overview and in
// Sistem › Bağlantı (with the three ways to get data); loading it is one deliberate request, a
// banner then says on every page that the values are invented, and one click removes them again.
// Where a source can fill the archive (Google connected, Garmin selected) nothing is offered.
const assert = require('node:assert/strict');
const fx = require('../../frontend/preview/fixtures.cjs');
const { BASE, launch, run, shot } = require('./lib.cjs');
const { contained } = require('./helpers/accessibility.cjs');

const none = fx.scenario('none');
const sample = fx.scenario('sample');

async function open(browser, { health = none.health, locale = 'tr-TR', viewport = { width: 1366, height: 768 }, refuse = false } = {}) {
  const context = await browser.newContext({ locale, viewport, timezoneId: 'Europe/Istanbul', reducedMotion: 'reduce' });
  const page = await context.newPage();
  const errors = [], calls = [];
  page.on('pageerror', error => errors.push(error.message));
  let loaded = false;
  await page.clock.setFixedTime(new Date(fx.NOW));
  await page.route('**/*', route => {
    const request = route.request(), url = new URL(request.url()), p = url.pathname;
    const json = (body, status = 200) => route.fulfill({ status, json: typeof body === 'function' ? body(url.searchParams) : body });
    const data = loaded ? sample : none;
    if (p === '/api/demo') {
      calls.push({ method: request.method(), query: url.search, marker: request.headers()['x-healthos-request'] });
      if (refuse) return json({ detail: 'Arşivde gerçek veri var; örnek veri yalnız boş bir kuruluma yüklenir.' }, 409);
      loaded = request.method() === 'POST';
      return json({ loaded, rows: loaded ? 13415 : 0, first_day: null, last_day: null });
    }
    if (p === '/health') return json(health);
    if (p === '/api/dashboard') return json(data.dashboard);
    if (p === '/api/dashboard/history') return json(data.history ?? fx.history);
    if (p === '/api/dashboard/measurements') return json([]);
    if (p === '/api/dashboard/workouts') return json(data.workouts);
    if (p === '/api/devices') return json(data.devices);
    if (/^\/(api|sync|auth|google-health)\//.test(p)) return route.fulfill({ status: 404, json: {} });
    return route.continue();
  });
  await page.routeWebSocket(/\/api\/codex\/chat$/, () => {});
  await page.goto(BASE);
  await page.waitForFunction(() => document.getElementById('service-status').textContent !== 'Kontrol ediliyor' &&
    document.querySelectorAll('#history-rows tr').length === 7);
  return { page, context, errors, calls };
}

const visible = (page, selector) => page.locator(selector).isVisible();
const textOf = (page, selector) => page.locator(selector).textContent();

run(async () => {
  const browser = await launch();
  try {
    // No source: the empty overview offers the sample next to "Ölçüm ekle"; loading it needs no
    // account, asks once with the deliberate-request header and the shown time zone, and the
    // banner replaces the empty state.
    for (const viewport of [{ width: 1366, height: 768 }, { width: 390, height: 844 }]) {
      const name = `${viewport.width}x${viewport.height}`;
      const { page, context, errors, calls } = await open(browser, { viewport });
      await page.locator('#overview-state[data-state="empty"]').waitFor();
      assert.equal(await textOf(page, '#overview-state-sample'), 'Örnek veriyle dene');
      assert.equal(await visible(page, '#sample-banner'), false);
      await contained(page);
      await page.locator('#overview-state-sample').click();
      await page.locator('#sample-banner:not([hidden])').waitFor();
      assert.deepEqual(calls, [{ method: 'POST', query: '?timezone=Europe%2FIstanbul', marker: '1' }]);
      assert.match(await textOf(page, '#sample-banner-text'), /^Örnek veri gösteriliyor: uydurma/);
      await page.waitForFunction(() => document.getElementById('overview-state').hidden);
      assert.notEqual(await textOf(page, '#steps-value'), '—');
      await contained(page);
      await page.screenshot({ path: shot(`sample-banner-${name}.png`) });
      // The banner stays on every page, and the sample's workouts say whose they are.
      await page.locator('[data-page="sports"]').click();
      await page.locator('.workout-source').first().waitFor();
      assert.equal(await visible(page, '#sample-banner'), true);
      assert.equal(await page.locator('.workout-source').first().textContent(), 'Örnek veri');
      await contained(page);
      // One click removes it; the empty state and its offer come back.
      await page.locator('#sample-remove').click();
      await page.waitForFunction(() => document.getElementById('sample-banner').hidden);
      assert.deepEqual(calls.map(call => [call.method, call.marker]), [['POST', '1'], ['DELETE', '1']]);
      await page.locator('[data-page="overview"]').click();
      await page.locator('#overview-state-sample:not([hidden])').waitFor();
      assert.deepEqual(errors, []);
      await context.close();
    }

    // Sistem › Bağlantı: the three ways to get data, each with its real next step.
    {
      const { page, context, calls } = await open(browser);
      await page.locator('[data-page="system"]').click();
      await page.locator('#source-guide:not([hidden])').waitFor();
      const ways = await page.locator('#source-guide li strong').allTextContents();
      assert.deepEqual(ways, ['Garmin', 'Google', 'Örnek veri']);
      const garmin = page.locator('#source-guide a');
      assert.equal(await garmin.getAttribute('href'), 'https://developer.garmin.com/gc-developer-program/personal/personal-api');
      assert.equal(await garmin.getAttribute('rel'), 'noopener noreferrer');
      assert.match(await textOf(page, '#source-guide'), /HEALTHOS_SOURCES=garmin[\s\S]*HEALTHOS_SOURCES=google/);
      assert.doesNotMatch(await textOf(page, '#source-guide'), /FIT|içe aktar/i, 'no import is promised before it exists');
      await page.locator('#source-guide-sample').click();
      await page.locator('#sample-banner:not([hidden])').waitFor();
      await page.waitForFunction(() => document.getElementById('source-guide-sample').textContent === 'Örnek veriyi kaldır');
      assert.equal(calls.length, 1);
      await contained(page);
      await page.screenshot({ path: shot('sample-guide-1366x768.png') });
      await context.close();
    }

    // A refusal (the archive already holds real data) is said where the button was pressed.
    {
      const { page, context } = await open(browser, { refuse: true });
      await page.locator('#overview-state-sample').click();
      await page.waitForFunction(() => /gerçek veri/.test(document.getElementById('overview-state-detail').textContent));
      assert.equal(await visible(page, '#sample-banner'), false);
      await context.close();
    }

    // English: the same offer and banner in English.
    {
      const { page, context } = await open(browser, { locale: 'en-US' });
      assert.equal(await textOf(page, '#overview-state-sample'), 'Try with sample data');
      await page.locator('#overview-state-sample').click();
      await page.locator('#sample-banner:not([hidden])').waitFor();
      assert.match(await textOf(page, '#sample-banner-text'), /^Showing sample data/);
      assert.equal(await textOf(page, '#sample-remove'), 'Remove sample data');
      await context.close();
    }

    // A source that can fill the archive: no offer, no guide.
    for (const health of [{ ...none.health, active_sources: ['garmin'] }, { ...none.health, active_sources: ['google'], configuration: 'healthy' }]) {
      const { page, context } = await open(browser, { health });
      await page.locator('#overview-state[data-state="empty"]').waitFor();
      assert.equal(await visible(page, '#overview-state-sample'), false, health.active_sources.join());
      await page.locator('[data-page="system"]').click();
      await page.waitForFunction(() => !document.getElementById('system').hidden);
      assert.equal(await visible(page, '#source-guide'), false, health.active_sources.join());
      await context.close();
    }
    console.log('PASS sample data: offered without a source, loaded and labelled, removed in one click');
  } finally { await browser.close(); }
});
