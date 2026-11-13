// The journey tests/e2e/run_journey.py drives: the built frontend against the real app and
// PostgreSQL, nothing answered from a fixture. Synthetic archive only (tests/e2e/seed_archive.py).
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { BASE, launch, run, shot } = require('../browser/lib.cjs');

const TOKEN = process.env.HEALTHOS_E2E_TOKEN;
const CANONICAL = process.env.HEALTHOS_E2E_CANONICAL_URL;
const WEIGHT = process.env.HEALTHOS_E2E_WEIGHT;
const archive = JSON.parse(fs.readFileSync(process.env.HEALTHOS_E2E_ARCHIVE, 'utf8'));
// The value as the Turkish interface prints it.
const WEIGHT_TEXT = WEIGHT.replace('.', ',');
const PAGES = ['measurements', 'system', 'sports'];
// A spread of the sizes tests/browser/viewport-floors.json states, from the smallest up.
const SIZES = [[320, 256], [320, 568], [375, 667], [390, 844], [844, 390], [1024, 600], [1366, 650], [1366, 768], [1920, 1080]];

const digits = text => text.replace(/\D/g, '');

function watch(page, problems) {
  page.on('pageerror', error => problems.push(`page error: ${error.message}`));
  page.on('console', message => {
    if (message.type() === 'error' && !message.text().includes('Failed to load resource')) problems.push(`console: ${message.text()}`);
  });
  page.on('response', response => {
    if (response.status() >= 500) problems.push(`${response.status()} from ${new URL(response.url()).pathname}`);
  });
}

async function login(page) {
  const live = await page.request.get(`${BASE}/health/live`, { headers: { Accept: 'text/html' }, maxRedirects: 0 });
  assert.equal(live.status(), 200, 'loopback liveness must not redirect');
  const response = await page.goto(BASE);
  await page.waitForURL(/\/login/);
  assert.equal(new URL(page.url()).origin, CANONICAL, 'the login form must be on the canonical host');
  let first = response.request();
  while (first.redirectedFrom()) first = first.redirectedFrom();
  assert.equal(first.url(), `${BASE}/`);
  const redirect = await first.response();
  assert.equal(redirect.status(), 307);
  assert.equal(redirect.headers().location, `${CANONICAL}/`, 'move before showing a login form');
  const address = await response.serverAddr();
  assert.equal(address.ipAddress, '127.0.0.1', 'Chromium must resolve healthos.localhost to loopback');
  console.log('PASS loopback redirects before login; healthos.localhost resolves to 127.0.0.1');
  await page.fill('#token', 'wrong-key-for-this-install');
  await page.click('button[type="submit"]');
  await page.locator('p.error[role="alert"]').waitFor();
  assert.equal(new URL(page.url()).pathname, '/login', 'a wrong key must stay on the login page');
  await page.fill('#token', TOKEN);
  await page.click('button[type="submit"]');
  await page.waitForURL(url => url.pathname === '/');
  assert.equal(new URL(page.url()).origin, CANONICAL);
  const cookies = await page.context().cookies();
  assert.ok(cookies.some(cookie => cookie.name === 'healthos_ui' && cookie.domain === 'healthos.localhost'));
  assert.ok(!cookies.some(cookie => ['127.0.0.1', 'localhost'].includes(cookie.domain)));
}

const panelLoaded = page => page.waitForFunction(() => document.querySelectorAll('#history-rows tr').length === 7);

function fitsOnOneScreen(page, label) {
  return page.evaluate(() => {
    const root = document.scrollingElement;
    const clipped = ['html', 'body'].map(tag => getComputedStyle(document.querySelector(tag)))
      .some(style => /hidden|clip/.test(style.overflowX + style.overflowY));
    return { w: innerWidth, h: innerHeight, sw: root.scrollWidth, sh: root.scrollHeight, clipped };
  }).then(size => {
    assert.ok(size.sw <= size.w && size.sh <= size.h, `${label}: the page scrolls ${JSON.stringify(size)}`);
    assert.equal(size.clipped, false, `${label}: html/body clip the page instead of fitting it`);
  });
}

async function showPage(page, id) {
  await page.evaluate(name => document.querySelector(`[data-page="${name}"]`).click(), id);
  await page.waitForFunction(name => !document.getElementById(name).hidden, id);
}

async function addMeasurement(page) {
  await page.click('#add-measurement');
  await page.waitForFunction(() => document.querySelector('#measurement-dialog').open);
  await page.selectOption('#measurement-metric', 'weight');
  await page.fill('#measurement-value', WEIGHT);
  const saved = page.waitForResponse(response => response.url().endsWith('/api/dashboard/measurements') && response.request().method() === 'POST');
  await page.click('#measurement-save');
  const response = await saved;
  assert.equal(response.status(), 201, `saving the measurement answered ${response.status()}`);
  await page.waitForFunction(() => !document.querySelector('#measurement-dialog').open);
}

async function measurementIsListed(page) {
  await showPage(page, 'measurements');
  await page.waitForFunction(text => document.querySelector('#measurement-rows').textContent.includes(text), WEIGHT_TEXT);
}

run(async () => {
  const browser = await launch();
  const problems = [];
  try {
    const context = await browser.newContext({ viewport: { width: 1366, height: 768 }, timezoneId: 'Europe/Istanbul', locale: 'tr-TR', reducedMotion: 'reduce' });
    const page = await context.newPage();
    watch(page, problems);

    await login(page);
    await panelLoaded(page);
    const shown = digits(await page.locator('#history-rows').textContent());
    assert.ok(shown.includes(String(archive.marker_steps)), `the panel does not show the archive's ${archive.marker_steps} steps`);
    console.log('PASS login and panel on the real archive');

    await addMeasurement(page);
    await measurementIsListed(page);
    console.log('PASS measurement saved through the real API');

    await page.reload();
    await page.waitForFunction(() => document.querySelector('#history-rows') !== null);
    assert.equal(new URL(page.url()).pathname, '/', 'the session must survive a reload');
    await page.waitForFunction(() => document.querySelectorAll('#history-rows tr').length === 7);
    await measurementIsListed(page);
    console.log('PASS measurement still there after a reload');

    for (const [width, height] of SIZES) {
      await page.setViewportSize({ width, height });
      await page.evaluate(() => document.querySelector('[data-page="overview"]').click());
      await page.waitForTimeout(150);
      await fitsOnOneScreen(page, `overview ${width}×${height}`);
      for (const id of PAGES) {
        await showPage(page, id);
        await page.waitForTimeout(150);
        await fitsOnOneScreen(page, `${id} ${width}×${height}`);
      }
    }
    console.log(`PASS one screen on the real archive at ${SIZES.length} sizes and ${PAGES.length + 1} pages`);

    await page.setViewportSize({ width: 1366, height: 768 });
    await page.evaluate(() => document.querySelector('[data-page="overview"]').click());
    await page.screenshot({ path: shot('e2e-journey.png') });
    assert.deepEqual(problems, [], `the journey reported problems:\n${problems.join('\n')}`);
    console.log('PASS no page errors, console errors or 5xx answers on the way');
  } finally {
    await browser.close();
  }
});
