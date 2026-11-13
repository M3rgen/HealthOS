// Synthetic fixtures. English mode: no page shows Turkish text. Texts that are data (the fixtures'
// own names) are excluded; everything else must come from the English dictionary.
const assert = require('node:assert/strict');
const fx = require('../../frontend/preview/fixtures.cjs');
const { BASE, launch, run } = require('./lib.cjs');
const { contained } = require('./helpers/accessibility.cjs');

// Fixture data shown as is: the point of the check is the interface's own words.
const DATA = /Yürüyüş|Koşu|Bisiklet|Yüzme|Ağırlık antrenmanı|Yoga/;
const TURKISH = /[çğıöşüÇĞİÖŞÜ]/;

async function open(context) {
  const page = await context.newPage();
  const errors = [], warnings = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => { if (message.text().startsWith('i18n:')) warnings.push(message.text()); });
  await page.clock.setFixedTime(new Date(fx.NOW));
  await page.addInitScript(() => localStorage.setItem('healthos.language', 'en'));
  await page.route('**/*', route => {
    const url = new URL(route.request().url());
    const p = url.pathname, q = url.searchParams;
    const json = body => route.fulfill({ json: typeof body === 'function' ? body(q) : body });
    if (p === '/health') return json(fx.health);
    if (p === '/api/dashboard') return json(fx.dashboard);
    if (p === '/api/dashboard/history') return json(fx.history);
    if (p === '/api/dashboard/measurements') return json(fx.measurements ?? []);
    if (p === '/api/dashboard/workouts') return json(fx.workouts);
    if (p === '/api/sync/coverage') return json(fx.coverage);
    if (p === '/api/webhooks/status') return json(fx.webhookStatus);
    if (p === '/api/devices') return json(fx.devices);
    if (p === '/sync/status') return json(fx.syncStatus);
    if (/^\/(api|sync|auth|google-health)\//.test(p)) return route.fulfill({ status: 404, json: {} });
    return route.continue();
  });
  await page.routeWebSocket(/\/api\/codex\/chat$/, () => {});
  return { page, errors, warnings };
}

// Turkish words without Turkish letters are caught by a short list of common interface words.
const WORDS = /\b(Bugün|Yeniden|Durum|Kayıt|Saat|Gün|Ayrıntı|Veri|Eşitle|Adım|Uyku|Nabız|Cihaz|Kapat|Kaydet|Antrenman)\b/;
const visible = page => page.evaluate(() => {
  const texts = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const node = walker.currentNode, element = node.parentElement, text = node.textContent.trim();
    if (!text || element.closest('script, style, svg')) continue;
    if (element.closest('[hidden]') || getComputedStyle(element).display === 'none' || getComputedStyle(element).visibility === 'hidden') continue;
    const dialog = element.closest('dialog');
    if (dialog && !dialog.open) continue;
    texts.push(text);
  }
  return texts;
});
const leftovers = texts => texts.filter(text => text !== 'Türkçe' && !DATA.test(text) && (TURKISH.test(text) || WORDS.test(text)));

run(async () => {
  const browser = await launch();
  try {
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, timezoneId: 'UTC', reducedMotion: 'reduce' });
    const { page, errors, warnings } = await open(context);
    await page.goto(BASE);
    await page.waitForFunction(() => document.querySelectorAll('#history-rows tr').length === 7);
    assert.equal(await page.evaluate(() => document.documentElement.lang), 'en');
    assert.deepEqual(leftovers(await visible(page)), [], 'overview');
    for (const name of ['measurements', 'system', 'sports']) {
      await page.locator(`[data-page="${name}"]`).click();
      await page.waitForTimeout(400);
      await page.waitForFunction(id => !document.getElementById(id).textContent.includes('Loading'), name === 'system' ? 'coverage-summary' : name === 'sports' ? 'workout-status' : 'measurement-rows');
      assert.deepEqual(leftovers(await visible(page)), [], name);
      await contained(page);
    }
    // The chat panel and the fixture-heavy dialogs.
    await page.locator('[data-page="overview"]').click();
    await page.locator('#assistant-open').click();
    await page.waitForFunction(() => !document.getElementById('assistant-dialog').hidden);
    assert.deepEqual(leftovers(await visible(page)), [], 'assistant');
    await page.locator('#assistant-close').click();
    await page.locator('[data-page="system"]').click();
    await page.locator('#coverage-open').click();
    assert.deepEqual(leftovers(await visible(page)), [], 'coverage dialog');
    await page.keyboard.press('Escape');
    await page.locator('#appearance-open').click();
    await page.waitForFunction(() => document.querySelectorAll('.preset-tile').length > 8);
    assert.deepEqual(leftovers(await visible(page)), [], 'appearance dialog');
    await page.keyboard.press('Escape');
    assert.deepEqual(warnings, [], 'no dictionary gap reported in the console');
    assert.deepEqual(errors, []);
    console.log('PASS English: every page, the chat and the coverage dialog are free of Turkish text');
    await context.close();
  } finally { await browser.close(); }
});
