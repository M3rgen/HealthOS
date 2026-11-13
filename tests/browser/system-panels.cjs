// Synthetic fixtures only; no live account records.
// Sistem fits the screen: the page itself does not scroll, every group scrolls inside itself, and
// no group nests another vertical scroller. Narrow or short screens get one group per tab.
const assert = require('node:assert/strict');
const fx = require('../../frontend/preview/fixtures.cjs');
const { BASE, launch, run } = require('./lib.cjs');

const SIZES = [[1920, 1080], [1366, 768], [1366, 650], [1280, 720], [1024, 600], [844, 390], [768, 1024], [390, 844], [375, 667], [320, 568]];

const openSystem = async (browser, [width, height]) => {
  const page = await browser.newPage({ viewport: { width, height }, reducedMotion: 'reduce', hasTouch: width <= 900 });
  await page.clock.setFixedTime(new Date(fx.NOW));
  await page.route('**/*', route => {
    const url = new URL(route.request().url()), p = url.pathname, q = url.searchParams;
    const json = body => route.fulfill({ json: typeof body === 'function' ? body(q) : body });
    if (p === '/health') return json(fx.health);
    if (p === '/api/dashboard') return json(fx.dashboard);
    if (p === '/api/devices') return json(fx.devices);
    if (p === '/api/sync/coverage') return json(fx.coverage);
    if (p === '/api/webhooks/status') return json(fx.webhookStatus);
    if (p === '/sync/status') return json(fx.syncStatus);
    if (p === '/api/settings/appearance') return json(fx.appearanceSettings);
    if (/^\/(api|sync|auth|google-health)\//.test(p)) return route.fulfill({ status: 404, json: {} });
    return route.continue();
  });
  await page.routeWebSocket(/\/api\/codex\/chat$/, () => {});
  await page.goto(`${BASE}/#/system`);
  await page.waitForFunction(() => document.querySelectorAll('#devices-list .device-row').length > 0);
  await page.waitForTimeout(250);
  return page;
};

const measure = page => page.evaluate(() => {
  const root = document.scrollingElement, system = document.getElementById('system');
  const vertical = el => /auto|scroll/.test(getComputedStyle(el).overflowY) && el.scrollHeight > el.clientHeight + 1;
  const groups = [...document.querySelectorAll('.system-group')].filter(el => !el.hidden);
  const nested = groups.flatMap(group => [...group.querySelectorAll('*')].filter(vertical).map(el => el.id || el.className));
  return {
    doc: root.scrollHeight <= innerHeight && root.scrollWidth <= innerWidth,
    system: `${system.scrollHeight}/${system.clientHeight}`, systemFits: system.scrollHeight <= system.clientHeight + 1,
    nested, tabbed: document.getElementById('system-panels').hasAttribute('data-tabbed'), visibleGroups: groups.length,
    heights: groups.map(group => group.clientHeight),
  };
});

run(async () => {
  const browser = await launch();
  try {
    for (const size of SIZES) {
      const page = await openSystem(browser, size);
      const label = size.join('x');
      const result = await measure(page);
      assert.ok(result.doc, `${label}: the document scrolls`);
      assert.ok(result.systemFits, `${label}: #system is itself a scroller (${result.system})`);
      assert.deepEqual(result.nested, [], `${label}: a scroller nested inside a group`);
      assert.ok(result.heights.every(height => height >= 120), `${label}: a group is too short to use: ${result.heights}`);
      if (result.tabbed) {
        assert.equal(result.visibleGroups, 1, `${label}: one group per tab`);
        for (const key of ['devices', 'sync', 'settings', 'status']) {
          await page.locator(`#system-tab-${key}`).click();
          await page.waitForTimeout(40);
          const shown = await measure(page);
          assert.ok(shown.systemFits && shown.doc, `${label} ${key}: page scrolls (${shown.system})`);
          assert.deepEqual(shown.nested, [], `${label} ${key}: nested scroller`);
          assert.equal(await page.locator(`[data-group="${key}"]`).isVisible(), true);
        }
        const covered = await page.evaluate(() => {
          const launcher = document.getElementById('assistant-open').getBoundingClientRect();
          const group = [...document.querySelectorAll('.system-group')].find(el => !el.hidden);
          const box = group.getBoundingClientRect();
          const walker = document.createTreeWalker(group, NodeFilter.SHOW_TEXT);
          const hits = [];
          while (walker.nextNode()) {
            if (!walker.currentNode.data.trim()) continue;
            const range = document.createRange();
            range.selectNodeContents(walker.currentNode);
            for (const r of range.getClientRects()) {
              const top = Math.max(r.top, box.top), bottom = Math.min(r.bottom, box.bottom);
              if (r.width && bottom > top && r.left < launcher.right && r.right > launcher.left && top < launcher.bottom && bottom > launcher.top) hits.push(walker.currentNode.data.trim());
            }
          }
          return hits;
        });
        assert.deepEqual(covered, [], `${label}: the launcher covers text at rest`);
        const tablist = page.locator('.system-tabs[role="tablist"]');
        assert.equal(await tablist.isVisible(), true);
        await page.locator('#system-tab-status').focus();
        await page.keyboard.press('ArrowRight');
        assert.equal(await page.evaluate(() => document.activeElement.id), 'system-tab-devices', `${label}: arrow keys move between tabs`);
        assert.equal(await page.locator('[data-group="devices"]').isVisible(), true);
      } else {
        assert.equal(result.visibleGroups, 4, `${label}: every group visible on a wide screen`);
        assert.equal(await page.locator('.system-tabs').isVisible(), false);
        // The last card of a group is reachable by scrolling only that group.
        const reachable = await page.evaluate(() => {
          const group = document.querySelector('[data-group="settings"]');
          group.scrollTop = group.scrollHeight;
          const about = document.querySelector('.about-panel').getBoundingClientRect(), box = group.getBoundingClientRect();
          return about.bottom <= box.bottom + 1;
        });
        assert.equal(reachable, true, `${label}: the last card reaches the group's edge`);
      }
      await page.close();
    }
    console.log(`PASS Sistem fits at ${SIZES.length} sizes: one scroller per group, no nesting`);
  } finally {
    await browser.close();
  }
});
