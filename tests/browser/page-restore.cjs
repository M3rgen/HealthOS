// Synthetic fixtures; no live account records.
// Back/forward cache: leaving the page stops the 60 s refresh and disposes the 3D model; coming
// back shows the model (or its silhouette), fetches the data at once and restarts the refresh.
// The 60 s interval is recorded in the page, so the check need not wait a minute; the restore is
// replayed with pagehide/pageshow events (persisted: true), and a real Back navigation is tried too.
const assert = require('node:assert/strict');
const fx = require('../../frontend/preview/fixtures.cjs');
const { BASE, launch, run, shot, until, showCard } = require('./lib.cjs');

async function setup(browser, viewport) {
  const page = await browser.newPage({ viewport });
  const state = { dashboards: 0 };
  await page.addInitScript(() => {
    const set = window.setInterval, clear = window.clearInterval;
    window.refreshTimers = new Map();
    window.setInterval = (callback, delay, ...args) => {
      const id = set(callback, delay, ...args);
      if (delay === 60000) window.refreshTimers.set(id, callback);
      return id;
    };
    window.clearInterval = id => { window.refreshTimers.delete(id); clear(id); };
    window.restores = [];
    addEventListener('pageshow', event => window.restores.push(event.persisted));
  });
  await page.route('**/*', route => {
    const url = new URL(route.request().url());
    if (url.pathname === '/away') return route.fulfill({ contentType: 'text/html', body: '<!doctype html><title>Başka sayfa</title><p>Başka sayfa</p>' });
    if (url.pathname === '/health') return route.fulfill({ json: fx.health });
    if (url.pathname === '/api/dashboard') { state.dashboards++; return route.fulfill({ json: fx.dashboard(url.searchParams) }); }
    if (url.pathname === '/sync/status') return route.fulfill({ json: fx.syncStatus });
    if (/^\/(api|sync|auth|google-health)\//.test(url.pathname)) return route.fulfill({ status: 404, json: {} });
    return route.continue();
  });
  await page.goto(BASE);
  await page.waitForFunction(() => document.querySelectorAll('#history-rows tr').length === 7);
  await showCard(page, 'profile');
  return { page, state };
}

// The model is drawn (a canvas over a loaded model) or the silhouette is visible.
const figure = page => page.evaluate(() => {
  const model = document.getElementById('body-model');
  const silhouette = model.querySelector('.model-fallback');
  const style = silhouette && getComputedStyle(silhouette);
  return {
    canvas: Boolean(model.querySelector('canvas')) && 'modelLoaded' in model.dataset,
    silhouette: Boolean(style && style.visibility === 'visible' && Number(style.opacity) > 0 && silhouette.getBoundingClientRect().height > 0),
  };
});
const timers = page => page.evaluate(() => window.refreshTimers.size);
const transition = (page, type) => page.evaluate(type => dispatchEvent(new PageTransitionEvent(type, { persisted: true })), type);

run(async () => {
  const browser = await launch();
  try {
    for (const viewport of [{ width: 1280, height: 800 }, { width: 390, height: 844 }]) {
      const { page, state } = await setup(browser, viewport);
      const name = `${viewport.width}x${viewport.height}`;
      assert.equal(await timers(page), 1, 'one 60 s refresh runs');
      await until(async () => (await figure(page)).canvas || (await figure(page)).silhouette, 'model or silhouette before leaving');

      await transition(page, 'pagehide');
      assert.equal(await timers(page), 0, 'leaving stops the refresh');
      assert.deepEqual(await figure(page), { canvas: false, silhouette: true }, 'a disposed model leaves the silhouette, not an empty box');
      const before = state.dashboards;
      await transition(page, 'pageshow');
      await until(() => state.dashboards > before, 'the restored page fetches its data at once');
      assert.equal(await timers(page), 1, 'the 60 s refresh runs again, once');
      const shown = await figure(page);
      assert.ok(shown.canvas || shown.silhouette, JSON.stringify(shown));
      await until(async () => (await figure(page)).canvas || (await page.evaluate(() => 'modelFailed' in document.getElementById('body-model').dataset)),
        'the model is built again after the restore');
      const polled = state.dashboards;
      await page.evaluate(() => [...window.refreshTimers.values()][0]());
      await until(() => state.dashboards > polled, 'the restarted refresh asks for new data');
      await page.screenshot({ path: shot(`page-restore-${name}.png`) });
      assert.equal(await page.evaluate(() => document.documentElement.scrollHeight > innerHeight || document.documentElement.scrollWidth > innerWidth), false);

      // A real Back navigation. Chromium restores from the cache when it can; otherwise the page
      // loads afresh, which must work just the same.
      await page.goto(new URL('/away', BASE).href);
      const left = state.dashboards;
      await page.goBack();
      await page.waitForFunction(() => document.querySelectorAll('#history-rows tr').length === 7);
      await showCard(page, 'profile');
      const restored = await page.evaluate(() => window.restores.at(-1));
      await until(() => state.dashboards > left, 'data after Back');
      assert.equal(await timers(page), 1);
      const back = await figure(page);
      assert.ok(back.canvas || back.silhouette, JSON.stringify(back));
      console.log(`${restored ? 'PASS' : 'INFO'} ${name}: Back ${restored ? 'restored the page from the back/forward cache' : 'reloaded the page (no back/forward cache here)'}`);
      console.log(`PASS ${name}: leaving stops the refresh and shows the silhouette; restoring fetches at once, restarts one refresh and rebuilds the model`);
      await page.close();
    }
  } finally {
    await browser.close();
  }
});
