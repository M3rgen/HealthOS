// Only fictional public sample content; never read a user's private plan file or backend.
const assert = require('node:assert/strict');
const fx = require('../../frontend/preview/fixtures.cjs');
const { BASE, launch, run, until, shot } = require('./lib.cjs');
const { contained, tabTo, visibleFocus } = require('./helpers/accessibility.cjs');
const { auditText: auditLayout } = require('./helpers/text-audit.cjs');
const { auditText: auditContrast, auditFocus } = require('./helpers/contrast.cjs');
const { server, mockPlan } = require('./helpers/plan.cjs');

const section = (page, key) => page.locator(`[data-section="${key}"]`).click();
async function saved(page, remote, count) {
  await until(() => remote.puts.length === count, 'Plan write received');
  await page.waitForFunction(() => /kaydedildi|saved/.test(document.querySelector('.plan-save-status').textContent));
}
async function open(browser, remote, viewport, language = 'tr', hash = '') {
  const page = await browser.newPage({ viewport, locale: language === 'en' ? 'en-US' : 'tr-TR', reducedMotion: 'reduce', timezoneId: 'UTC' });
  const errors = [], scripts = [];
  page.on('request', request => { if (/\/plan[.-][^/?]*\.js(?:\?|$)/.test(request.url())) scripts.push(request.url()); });
  page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(lang => {
    localStorage.setItem('healthos.language', lang);
    localStorage.setItem('healthos.timezone.v2', 'UTC');
  }, language);
  await page.clock.setFixedTime(new Date(fx.NOW));
  await mockPlan(page, remote);
  await page.goto(`${BASE}/${hash}`);
  await page.waitForFunction(() => document.documentElement.dataset.booted === 'true');
  return { page, errors, scripts };
}
async function audit(page) {
  await page.evaluate(() => document.fonts.ready);
  await contained(page);
  const layout = await page.evaluate(auditLayout);
  assert.deepEqual(layout.overflow, [], 'all document text remains reachable');
  assert.deepEqual(layout.overlap, [], 'no text collisions');
  const semantics = await page.locator('#plan').evaluate(root => {
    const visible = node => node.getClientRects().length > 0;
    return {
      h1: root.querySelectorAll('h1').length,
      unnamed: [...root.querySelectorAll('button, input')].filter(visible).filter(node =>
        !(node.textContent.trim() || node.getAttribute('aria-label') || node.labels?.length)).length,
      tables: [...root.querySelectorAll('thead th')].filter(node => node.getAttribute('scope') !== 'col').length,
      controls: [...root.querySelectorAll('[aria-controls]')].filter(node =>
        !document.getElementById(node.getAttribute('aria-controls'))).length,
    };
  });
  assert.deepEqual(semantics, { h1: 1, unnamed: 0, tables: 0, controls: 0 });
}
async function details(page) {
  await page.locator('#plan-day').fill('2026-10-12');
  await page.locator('#plan-day').press('Tab');
  const toggle = page.locator('.plan-meal [aria-expanded]').first();
  await tabTo(page, '.plan-meal [aria-expanded]');
  await page.keyboard.press('Enter');
  assert.equal(await toggle.getAttribute('aria-expanded'), 'true');
  const id = await toggle.getAttribute('aria-controls');
  assert.equal(await page.locator(`#${id}`).isVisible(), true);
  await visibleFocus(page);
  await page.keyboard.press('Space');
  assert.equal(await toggle.getAttribute('aria-expanded'), 'false');
  assert.equal(await page.locator(`#${id}`).isVisible(), false);
  await page.locator('.plan-disclosure > button').last().click();
  assert.equal(await page.locator('.plan-current').count(), 1);
  await audit(page);
}
async function shopping(page, remote, browser, viewport, language) {
  await section(page, 'shopping');
  assert.match(await page.locator('.plan-content').textContent(), /30 EUR/);
  const input = page.locator('[data-item-id="example-grain"]');
  await input.check();
  assert.equal(await input.isChecked(), true);
  await saved(page, remote, 1);
  await page.reload();
  await page.locator('[data-section="shopping"]').waitFor();
  await section(page, 'shopping');
  assert.equal(await input.isChecked(), true, 'reload reads server state');
  const phone = await open(browser, remote, viewport, language, '#/plan');
  await phone.page.locator('[data-section="shopping"]').waitFor();
  await section(phone.page, 'shopping');
  assert.equal(await phone.page.locator('[data-item-id="example-grain"]').isChecked(), true, 'another browser reads server state');
  await phone.page.close();
  page.once('dialog', dialog => dialog.dismiss());
  await page.locator('#plan-reset').click();
  assert.equal(await input.isChecked(), true);
  assert.equal(remote.puts.length, 1, 'dismissed confirmation sends no write');
  page.once('dialog', dialog => dialog.accept());
  await page.locator('#plan-reset').click();
  await saved(page, remote, 2);
  assert.equal(await input.isChecked(), false);
  remote.failWrite = true;
  await input.check();
  await page.locator('#notice:not([hidden])').waitFor();
  await until(async () => !(await input.isChecked()), 'failed optimistic check rolls back');
  await page.locator('#notice-close').click();
  remote.failWrite = false;
  await audit(page);
}
// Chrome commits every typed segment; a redraw that rebuilt the field would split "11" into two "1"s.
async function typedDate(page, remote) {
  const input = page.locator('#plan-start');
  await input.focus();
  await page.keyboard.type('11');
  const typed = await input.inputValue();
  assert.ok(['2026-10-11', '2026-11-01'].includes(typed), `two digits fill the first segment: ${typed}`);
  assert.equal(await page.evaluate(() => document.activeElement?.id), 'plan-start');
  await until(() => remote.view.state.start_date === typed, 'typed start date saved');
  await page.waitForFunction(() => /kaydedildi|saved/.test(document.querySelector('.plan-save-status').textContent));
}
async function datesAndSections(page, remote) {
  await section(page, 'today');
  await page.locator('#plan-start').fill('2026-10-01');
  await page.locator('#plan-start').press('Tab');
  await saved(page, remote, 4);
  assert.equal(remote.view.state.start_date, '2026-10-01');
  await page.reload();
  await page.locator('#plan-start').waitFor();
  assert.equal(await page.locator('#plan-start').inputValue(), '2026-10-01');
  await typedDate(page, remote);
  for (const key of ['nutrition', 'batch', 'training', 'assumptions', 'measurements']) {
    await section(page, key);
    await audit(page);
    await auditContrast(page, `Plan ${key}`);
  }
  await page.locator('.plan-content button').click();
  await page.locator('#measurement-dialog[open]').waitFor();
  await page.keyboard.press('Escape');
  await contained(page);
}
async function edgeCases(browser) {
  const remote = server();
  remote.view = { plan: null };
  const { page } = await open(browser, remote, { width: 390, height: 844 }, 'en', '#/plan');
  await page.locator('#plan code').waitFor();
  assert.match(await page.locator('#plan').textContent(), /docs\/reference\/plan.md/);
  assert.equal(await page.locator('[data-section]').count(), 0);
  await audit(page);
  remote.view = { plan: { schema: 1, measurements: ['Synthetic measurement instruction'] }, version: 2 };
  await page.locator('[data-page="plan"]').click();
  await page.locator('[data-section="measurements"]').waitFor();
  assert.deepEqual(await page.locator('[data-section]').evaluateAll(nodes => nodes.map(node => node.dataset.section)), ['measurements']);
  remote.view = { plan: { schema: 1 }, version: 3 };
  await page.locator('[data-page="plan"]').click();
  await page.getByText('This document has no sections to display yet.').waitFor();
  assert.equal(await page.locator('[data-section]').count(), 0);
  remote.failRead = true;
  await page.reload();
  await page.getByRole('button', { name: 'Try again', exact: true }).waitFor();
  remote.failRead = false;
  remote.view = structuredClone(fx.plan);
  remote.view.plan.meta.title = '<img src=x onerror="window.planUnsafe=true">';
  await page.getByRole('button', { name: 'Try again', exact: true }).click();
  await page.locator('[data-section="today"]').waitFor();
  assert.equal(await page.locator('#plan h1').textContent(), remote.view.plan.meta.title);
  assert.equal(await page.locator('#plan img').count(), 0);
  assert.equal(await page.evaluate(() => window.planUnsafe), undefined);
  await audit(page);
  await page.close();
}
// Plan's stylesheet loads with its chunk; nothing of the page is built before the sheet arrives.
async function styledFirstPaint(browser) {
  const remote = server();
  const { page, errors } = await open(browser, remote, { width: 1280, height: 800 });
  let release, requested = false;
  const held = new Promise(resolve => { release = resolve; });
  await page.route(/\/plan-[^/]*\.css(?:\?|$)/, async route => { requested = true; await held; await route.continue(); });
  await page.locator('[data-page="plan"]').click();
  await until(() => requested, 'Plan stylesheet requested');
  assert.equal(await page.locator('#plan .plan-content').count(), 0, 'no Plan content before its stylesheet');
  release();
  await page.locator('[data-section="today"]').waitFor();
  assert.equal(await page.locator('.plan-content').evaluate(node => getComputedStyle(node).overflowY), 'auto');
  assert.deepEqual(errors, []);
  await page.close();
}
run(async () => {
  const browser = await launch();
  try {
    for (const viewport of [{ width: 1280, height: 800 }, { width: 390, height: 844 }, { width: 360, height: 640 }]) {
      for (const language of ['tr', 'en']) {
        const remote = server();
        const { page, errors, scripts } = await open(browser, remote, viewport, language);
        assert.equal(remote.gets, 0, 'Plan is not fetched before its page opens');
        assert.deepEqual(scripts, [], 'Plan JavaScript stays deferred');
        assert.equal(await page.locator('.plan-content').count(), 0, 'Plan has not been initialized');
        await tabTo(page, '[data-page="plan"]');
        await page.keyboard.press('Enter');
        await page.locator('[data-section="today"]').waitFor();
        assert.equal(await page.locator('[data-section]').count(), 7);
        assert.ok(scripts.length > 0, 'Plan code loads when its tab opens');
        await details(page);
        await shopping(page, remote, browser, viewport, language);
        await datesAndSections(page, remote);
        await section(page, 'shopping');
        await tabTo(page, '[data-item-id="example-grain"]');
        await auditFocus(page, 'Plan checkbox');
        await page.screenshot({ path: shot(`plan-${viewport.width}-${language}.png`) });
        assert.deepEqual(errors, []);
        await page.close();
        console.log(`PASS Plan ${viewport.width}x${viewport.height} ${language}: keyboard, sections, state, totals, layout, accessibility`);
      }
    }
    await edgeCases(browser);
    await styledFirstPaint(browser);
  } finally { await browser.close(); }
});
