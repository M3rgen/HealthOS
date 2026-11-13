// Synthetic fixtures; no live account records.
// #profile-values and #calendar-month are aria-live: a refresh with identical data must not touch
// them, or a screen reader repeats them every 60 seconds. Changed data still does.
const assert = require('node:assert/strict');
const fx = require('../../frontend/preview/fixtures.cjs');
const { BASE, launch, run, until } = require('./lib.cjs');

run(async () => {
  const browser = await launch();
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
    const state = { dashboards: 0, weight: null };
    await page.route('**/*', route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/health') return route.fulfill({ json: fx.health });
      if (url.pathname === '/api/dashboard') {
        state.dashboards++;
        const data = fx.dashboard(url.searchParams);
        if (state.weight) data.profile.weight = { ...data.profile.weight, value: state.weight };
        return route.fulfill({ json: data });
      }
      if (url.pathname === '/sync/status') return route.fulfill({ json: fx.syncStatus });
      if (/^\/(api|sync|auth|google-health)\//.test(url.pathname)) return route.fulfill({ status: 404, json: {} });
      return route.continue();
    });
    await page.goto(BASE);
    await page.waitForFunction(() => document.querySelector('#profile-values button')?.textContent.trim().length > 0);
    const refreshed = async () => {
      const before = state.dashboards;
      await page.evaluate(() => {
        dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true }));
      });
      await until(() => state.dashboards > before, 'a refresh ran');
      // Painting follows the response; a task later the observers have their records.
      await page.waitForFunction(() => document.querySelectorAll('#history-rows tr').length === 7);
      await page.waitForTimeout(250);
    };
    await refreshed(); // Settle anything only the first paint writes.
    await page.evaluate(() => {
      window.records = { 'profile-values': 0, 'calendar-month': 0 };
      for (const id of Object.keys(window.records))
        new MutationObserver(list => { window.records[id] += list.length; })
          .observe(document.getElementById(id), { subtree: true, childList: true, characterData: true, attributes: true });
    });
    await refreshed();
    assert.deepEqual(await page.evaluate(() => window.records), { 'profile-values': 0, 'calendar-month': 0 });
    state.weight = 123.4;
    await refreshed();
    assert.ok((await page.evaluate(() => window.records['profile-values'])) > 0, 'changed data still reaches the live region');
    console.log('PASS: identical refresh leaves the live regions untouched, a change does not');
  } finally {
    await browser.close();
  }
});
