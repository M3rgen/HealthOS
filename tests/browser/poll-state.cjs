// Synthetic fixtures only; no live account records.
// A poll repaints the same data every 60 seconds. Driven through the real interval callback (the
// page's own setInterval, on Playwright's clock), it must leave the reader where they were: scroll
// position, open <details>, expanded rows and keyboard focus. Live regions must not hear unchanged text.
const assert = require('node:assert/strict');
const fx = require('../../frontend/preview/fixtures.cjs');
const { BASE, launch, run, until } = require('./lib.cjs');

const POLL_MS = 60_000;

async function openPage(browser, viewport) {
  const page = await browser.newPage({ viewport, reducedMotion: 'reduce', hasTouch: viewport.width <= 900 });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  const state = { dashboards: 0, devices: fx.devices, extraWorkout: false };
  await page.clock.install({ time: new Date(fx.NOW) });
  await page.route('**/*', route => {
    const url = new URL(route.request().url()), p = url.pathname, q = url.searchParams;
    const json = body => route.fulfill({ json: typeof body === 'function' ? body(q) : body });
    if (p.endsWith('.glb')) return route.abort();
    if (p === '/health') return json(fx.health);
    if (p === '/api/dashboard') { state.dashboards++; return json(fx.dashboardWithSources); }
    if (p === '/api/dashboard/history') return json(fx.history);
    if (p === '/api/dashboard/measurements') return json(fx.measurements);
    if (p === '/api/dashboard/workouts') {
      const rows = fx.workouts(q);
      return route.fulfill({ json: state.extraWorkout ? [...rows, { ...rows[0], id: 'polled-new', start_time: rows.at(-1).start_time, end_time: rows.at(-1).end_time }] : rows });
    }
    if (p === '/api/sync/coverage') return json(fx.coverage);
    if (p === '/api/webhooks/status') return json(fx.webhookStatus);
    if (p === '/api/devices') return json(state.devices);
    if (p === '/sync/status') return json(fx.syncStatus);
    if (p === '/api/settings/appearance') return json(fx.appearanceSettings);
    if (/^\/(api|sync|auth|google-health)\//.test(p)) return route.fulfill({ status: 404, json: {} });
    return route.continue();
  });
  await page.routeWebSocket(/\/api\/codex\/chat$/, () => {});
  await page.goto(BASE);
  await page.waitForFunction(() => document.querySelectorAll('#history-rows tr').length >= 7);
  await page.waitForTimeout(300);
  return { page, state, errors };
}

/** The page's own 60-second interval fires; waits until the poll asked for new data and painted it. */
async function poll(page, state) {
  const before = state.dashboards;
  await page.clock.fastForward(POLL_MS);
  await until(() => state.dashboards > before, 'the polling interval fired');
  await page.waitForTimeout(500);
}

const show = async (page, id) => {
  await page.locator(`[data-page="${id}"]`).click();
  await page.waitForTimeout(400);
};

const watchLiveRegions = page => page.evaluate(() => {
  window.liveRecords = {};
  for (const element of document.querySelectorAll('[aria-live]:not([aria-live="off"]), [role="status"], [role="alert"]')) {
    const name = element.id || element.className;
    window.liveRecords[name] = 0;
    new MutationObserver(list => { window.liveRecords[name] += list.length; })
      .observe(element, { subtree: true, childList: true, characterData: true });
  }
});
const liveWrites = page => page.evaluate(() => {
  const written = Object.fromEntries(Object.entries(window.liveRecords).filter(([, count]) => count > 0));
  for (const name of Object.keys(window.liveRecords)) window.liveRecords[name] = 0;
  return written;
});

run(async () => {
  const browser = await launch();
  try {
    const { page, state, errors } = await openPage(browser, { width: 1366, height: 768 });
    await watchLiveRegions(page);

    // Spor: scroll, an open <details> and focus survive an unchanged poll and a changed one.
    await show(page, 'sports');
    await page.locator('#workout-timeline summary').first().click();
    await page.evaluate(() => { document.getElementById('workout-timeline').scrollTop = 500; });
    const sports = () => page.evaluate(() => ({
      open: document.querySelectorAll('#workout-timeline details[open]').length,
      scroll: document.getElementById('workout-timeline').scrollTop,
      focus: document.activeElement === window.sportsFocus,
    }));
    await page.evaluate(() => { window.sportsFocus = document.querySelector('#workout-timeline details[open] summary'); window.sportsFocus.focus(); });
    const before = await sports();
    assert.deepEqual([before.open, before.focus], [1, true]);
    assert.ok(before.scroll > 0, 'the timeline scrolled');
    await liveWrites(page);
    await poll(page, state);
    assert.deepEqual(await sports(), before, 'an unchanged poll kept the sports list as it was');
    assert.deepEqual(await liveWrites(page), {}, 'an unchanged poll re-announced a live region');
    state.extraWorkout = true;
    await poll(page, state);
    assert.deepEqual(await sports(), before, 'a poll with a new session kept the list where it was');
    assert.equal(await page.locator('[data-workout-id="polled-new"]').count(), 1, 'the new session arrived');
    assert.match((await liveWrites(page))['workout-status'] ? 'written' : 'silent', /written/, 'a changed count reaches its live region');
    console.log('PASS Sports keeps scroll, open details and focus through the real poll; new data still arrives');

    // Overview: an expanded source row and focus on its toggle.
    await show(page, 'overview');
    await page.locator('#history-rows .source-toggle').first().click();
    const history = () => page.evaluate(() => {
      const toggle = document.querySelector('#history-rows .source-toggle');
      return {
        expanded: toggle.getAttribute('aria-expanded'), rowHidden: document.getElementById(toggle.getAttribute('aria-controls')).hidden,
        focus: document.activeElement === toggle,
      };
    });
    await page.locator('#history-rows .source-toggle').first().focus();
    await liveWrites(page);
    await poll(page, state);
    assert.deepEqual(await history(), { expanded: 'true', rowHidden: false, focus: true });
    assert.deepEqual(await liveWrites(page), {}, 'overview poll re-announced a live region');
    console.log('PASS Overview keeps an expanded source row and its focus through the real poll');

    // Sistem: scroll inside a group and focus on a device button.
    await show(page, 'system');
    await page.waitForFunction(() => document.querySelectorAll('#devices-list .device-row').length > 0);
    await page.evaluate(() => {
      document.querySelector('[data-group="devices"]').scrollTop = 30;
      window.systemFocus = document.querySelector('.device-details');
      window.systemFocus.focus();
    });
    const system = () => page.evaluate(() => ({
      scroll: document.querySelector('[data-group="devices"]').scrollTop,
      focus: document.activeElement === window.systemFocus,
    }));
    const beforeSystem = await system();
    assert.equal(beforeSystem.focus, true);
    assert.ok(beforeSystem.scroll > 0, 'the devices group scrolled');
    await liveWrites(page);
    await poll(page, state);
    assert.deepEqual(await system(), beforeSystem, 'the system page kept scroll and focus');
    assert.deepEqual(await liveWrites(page), {}, 'system poll re-announced a live region');
    state.devices = { ...fx.devices, devices: fx.devices.devices.slice(0, -1) };
    await poll(page, state);
    assert.deepEqual((await system()).focus, true, 'focus stayed while another device left the list');
    assert.ok((await liveWrites(page))['devices-summary'] > 0, 'a changed summary reaches its live region');
    console.log('PASS System keeps scroll and focus; live regions are written only on change');
    assert.deepEqual(errors, []);
    await page.close();
  } finally {
    await browser.close();
  }
});
