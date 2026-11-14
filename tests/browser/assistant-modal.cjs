// Synthetic fixtures. On a phone the Umay panel is a modal: the page behind it is inert, Tab stays
// inside, and closing gives the page and the focus back. A wide screen keeps its side panel.
const assert = require('node:assert/strict');
const fx = require('../../frontend/preview/fixtures.cjs');
const { BASE, launch, run, shot } = require('./lib.cjs');
const { contained } = require('./helpers/accessibility.cjs');

async function mock(page) {
  await page.route('**/*', route => {
    const url = new URL(route.request().url());
    const p = url.pathname;
    if (p === '/health') return route.fulfill({ json: fx.health });
    if (p === '/api/dashboard') return route.fulfill({ json: fx.dashboard(url.searchParams) });
    if (/^\/(api|sync|auth|google-health)\//.test(p)) return route.fulfill({ status: 404, json: {} });
    return route.continue();
  });
  await page.routeWebSocket(/\/api\/codex\/chat$/, socket => { socket.send(JSON.stringify({ type: 'ready' })); });
}
const backgroundInert = page => page.evaluate(() => ['header.topbar', 'main'].every(selector => document.querySelector(selector).inert));
const focusInside = page => page.evaluate(() => document.getElementById('assistant-dialog').contains(document.activeElement));

run(async () => {
  const browser = await launch();
  try {
    for (const viewport of [...[375, 700, 701, 800, 900].map(width => ({ width, height: 667 })), { width: 1024, height: 390 }]) {
      const page = await browser.newPage({ viewport, reducedMotion: 'reduce' });
      const errors = []; page.on('pageerror', error => errors.push(error.message));
      await mock(page);
      await page.goto(BASE);
      await page.waitForFunction(() => document.querySelectorAll('#history-rows tr').length === 7);
      await page.locator('#assistant-open').click();
      await page.waitForFunction(() => !document.getElementById('assistant-dialog').hidden);
      await page.waitForFunction(() => document.activeElement.closest('#assistant-dialog'));
      assert.equal(await backgroundInert(page), true);
      assert.equal(await page.locator('#assistant-dialog').getAttribute('aria-modal'), 'true');
      assert.equal(await page.locator('#assistant-dialog').getAttribute('role'), 'dialog');
      const box = await page.locator('#assistant-dialog').boundingBox();
      // Up to 900 px wide the panel is a full-screen sheet (chat-assistant.css); wider modals keep an 8 px margin.
      if (viewport.width <= 900) assert.ok(box.y === 0 && box.height === viewport.height && box.x === 0 && box.width === viewport.width, JSON.stringify(box));
      else assert.ok(box.y >= 8 && box.y + box.height <= viewport.height - 8 && box.height >= viewport.height - 16.5, JSON.stringify(box));
      assert.equal(await page.locator('#assistant-open').isVisible(), false, 'launcher hidden under the modal');
      assert.equal(await page.locator('#assistant-open').evaluate(el => el.inert), true, 'launcher out of the tab order under the modal');
      await page.locator('#assistant-open').focus().catch(() => {});
      assert.notEqual(await page.evaluate(() => document.activeElement.id), 'assistant-open', 'launcher cannot take focus under the modal');
      await page.locator('#assistant-messages').evaluate(el => {
        const message = document.createElement('p');
        message.className = 'chat-message';
        message.textContent = 'Synthetic long message. '.repeat(1000);
        el.append(message);
        el.scrollTop = el.scrollHeight;
      });
      assert.ok(await page.locator('#assistant-messages').evaluate(el => el.scrollTop > 0));
      await contained(page);
      if (viewport.width === 800) await page.screenshot({ path: shot('umay-800.png') });
      const seen = new Set();
      for (let i = 0; i < 12; i++) {
        await page.keyboard.press('Tab');
        // Tab may leave for the browser chrome; it must never land in the page behind.
        const place = await page.evaluate(() => {
          const el = document.activeElement;
          return el === document.body ? 'body' : el.closest('#assistant-dialog') ? (el.id || el.tagName) : `outside:${el.id || el.tagName}`;
        });
        assert.ok(!place.startsWith('outside:'), `Tab reached the page behind: ${place}`);
        seen.add(place);
      }
      assert.ok(seen.has('assistant-close'), 'Tab reaches the close button');
      await page.keyboard.press('Escape');
      await page.waitForFunction(() => document.getElementById('assistant-dialog').hidden);
      assert.equal(await backgroundInert(page), false, 'the page is usable again');
      assert.equal(await page.evaluate(() => document.activeElement.id), 'assistant-open');
      assert.equal(await page.locator('#assistant-open').isVisible(), true);
      await contained(page);
      assert.deepEqual(errors, []);
      console.log(`PASS ${viewport.width}x${viewport.height}: modal, inert background, Tab stays inside, focus returns`);
      await page.close();
    }
    // Height boundaries shared with CSS, including the newly widened layout ranges.
    for (const width of [390, 901, 1024]) for (const height of [480, 490, 500, 560, 580, 608]) {
      const page = await browser.newPage({ viewport: { width, height }, reducedMotion: 'reduce' });
      await mock(page);
      await page.goto(BASE);
      await page.waitForFunction(() => document.querySelectorAll('#history-rows tr').length === 7);
      await page.locator('#assistant-open').click();
      await page.locator('#assistant-dialog').waitFor();
      assert.equal(await backgroundInert(page), width <= 900 || height <= 480, `${width}x${height} Umay mode`);
      await contained(page);
      await page.locator('#assistant-close').click();
      await page.locator('[data-page="system"]').click();
      const tabbed = width <= 900 || height <= 560;
      await page.waitForFunction(expected => document.getElementById('system-panels').hasAttribute('data-tabbed') === expected, tabbed);
      if (tabbed) {
        await page.locator('#system-tab-settings').waitFor({ state: 'visible' });
        await page.locator('#system-tab-settings').click();
        await page.locator('[data-group="settings"]').waitFor({ state: 'visible' });
      }
      await contained(page);
      console.log(`PASS ${width}x${height}: Umay and system modes match CSS heights; no page overflow`);
      await page.close();
    }
    const page = await browser.newPage({ viewport: { width: 1280, height: 800 }, reducedMotion: 'reduce' });
    await mock(page);
    await page.goto(BASE);
    await page.waitForFunction(() => document.querySelectorAll('#history-rows tr').length === 7);
    await page.locator('#assistant-open').click();
    await page.waitForFunction(() => !document.getElementById('assistant-dialog').hidden);
    assert.equal(await backgroundInert(page), false, 'desktop stays a side panel');
    assert.equal(await page.locator('#assistant-dialog').getAttribute('aria-modal'), null);
    // Crossing the shared boundary with the panel open changes mode in both directions.
    await page.setViewportSize({ width: 900, height: 667 });
    await page.waitForFunction(() => document.querySelector('main').inert);
    await contained(page);
    await page.setViewportSize({ width: 901, height: 667 });
    await page.waitForFunction(() => !document.querySelector('main').inert);
    console.log('PASS desktop stays a side panel and the mode follows a resize');
    await page.close();
  } finally { await browser.close(); }
});
