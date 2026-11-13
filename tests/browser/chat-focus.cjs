// Synthetic fixtures only; no live account records.
// The desktop Umay panel is not modal: the page behind stays usable, but Tab must not stop on a
// control hidden under the panel, in either direction, and those controls stay usable afterwards.
const assert = require('node:assert/strict');
const fx = require('../../frontend/preview/fixtures.cjs');
const { BASE, launch, run } = require('./lib.cjs');

const covered = () => {
  const element = document.activeElement;
  const box = element.getBoundingClientRect();
  const hit = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2);
  return Boolean(hit?.closest('#assistant-dialog')) && !element.closest('#assistant-dialog');
};

run(async () => {
  const browser = await launch();
  try {
    const page = await browser.newPage({ viewport: { width: 1366, height: 768 }, reducedMotion: 'reduce' });
    await page.clock.setFixedTime(new Date(fx.NOW));
    await page.route('**/*', route => {
      const url = new URL(route.request().url()), p = url.pathname, q = url.searchParams;
      const json = body => route.fulfill({ json: typeof body === 'function' ? body(q) : body });
      if (p === '/health') return json(fx.health);
      if (p === '/api/dashboard') return json(fx.dashboard);
      if (p === '/api/settings/appearance') return json(fx.appearanceSettings);
      if (/^\/(api|sync|auth|google-health)\//.test(p)) return route.fulfill({ status: 404, json: {} });
      return route.continue();
    });
    await page.routeWebSocket(/\/api\/codex\/chat$/, () => {});
    await page.goto(BASE);
    await page.waitForFunction(() => document.querySelectorAll('#history-rows tr').length === 7);
    await page.locator('#assistant-open').click();
    await page.waitForTimeout(150);

    const walk = async key => {
      const stops = [];
      for (let i = 0; i < 40; i++) {
        await page.keyboard.press(key);
        stops.push(await page.evaluate(() => document.activeElement.id || document.activeElement.className || document.activeElement.tagName));
        assert.equal(await page.evaluate(covered), false, `${key} stopped on a control under the panel: ${stops.at(-1)}`);
      }
      return stops;
    };
    const forward = await walk('Tab');
    assert.ok(forward.some(name => name === 'assistant-close'), 'Tab still reaches the panel');
    assert.ok(forward.some(name => name.startsWith('profile-')), 'Tab still reaches the page beside the panel');
    await walk('Shift+Tab');
    console.log('PASS Tab and Shift+Tab pass over controls hidden by the desktop panel');

    // The skip lasts one key press: nothing stays inert, and a click on the page still works.
    await page.waitForTimeout(50);
    assert.equal(await page.evaluate(() => document.querySelectorAll('main [inert], header [inert]').length), 0, 'no control stays inert');
    await page.locator('[data-page="measurements"]').click();
    await page.waitForFunction(() => !document.getElementById('measurements').hidden);
    assert.equal(await page.evaluate(() => document.getElementById('assistant-dialog').hidden), false, 'the panel stays open: non-modal');
    console.log('PASS the page behind stays usable; nothing is left inert');
    await page.close();
  } finally {
    await browser.close();
  }
});
