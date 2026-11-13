// Synthetic fixtures; no live account records.
// The open page lives in the URL: Back and Forward move between pages inside the app, a reload or
// a deep link keeps the page, the tab title follows it, and none of it scrolls the document.
const assert = require('node:assert/strict');
const fx = require('../../frontend/preview/fixtures.cjs');
const { BASE, launch, run, until } = require('./lib.cjs');

async function setup(browser, viewport, path = '/') {
  const page = await browser.newPage({ viewport });
  const state = { measurements: 0, workouts: 0 };
  await page.route('**/*', route => {
    const url = new URL(route.request().url());
    if (url.pathname === '/health') return route.fulfill({ json: fx.health });
    if (url.pathname === '/api/dashboard') return route.fulfill({ json: fx.dashboard(url.searchParams) });
    if (url.pathname === '/api/dashboard/measurements') { state.measurements++; return route.fulfill({ json: [] }); }
    if (url.pathname === '/api/dashboard/workouts') { state.workouts++; return route.fulfill({ json: [] }); }
    if (url.pathname === '/sync/status') return route.fulfill({ json: fx.syncStatus });
    if (/^\/(api|sync|auth|google-health)\//.test(url.pathname)) return route.fulfill({ status: 404, json: {} });
    return route.continue();
  });
  await page.goto(`${BASE}${path}`);
  await page.waitForFunction(() => document.querySelectorAll('#history-rows tr').length === 7);
  await page.evaluate(() => { window.sameDocument = true; });
  return { page, state };
}

const snapshot = page => page.evaluate(() => ({
  hash: location.hash,
  title: document.title,
  visible: [...document.querySelectorAll('.page')].filter(el => !el.hidden).map(el => el.id),
  current: document.querySelector('[data-page][aria-current="page"]')?.dataset.page,
  sameDocument: Boolean(window.sameDocument),
  scroll: [window.scrollX, window.scrollY, document.documentElement.scrollTop, document.body.scrollTop],
}));

async function expectPage(page, name, hash, title, sameDocument = true) {
  await until(async () => (await snapshot(page)).current === name, `${name} becomes the current page`);
  const now = await snapshot(page);
  assert.deepEqual(now.visible, [name]);
  assert.equal(now.hash, hash);
  assert.match(now.title, title);
  assert.equal(now.sameDocument, sameDocument, 'stayed in the same document');
  assert.deepEqual(now.scroll, [0, 0, 0, 0], 'the document never scrolls');
}

run(async () => {
  const browser = await launch();
  try {
    for (const viewport of [{ width: 1280, height: 800 }, { width: 390, height: 844 }]) {
      const { page, state } = await setup(browser, viewport);
      await expectPage(page, 'overview', '', /Genel bakış/);

      await page.locator('[data-page="sports"]').click();
      await expectPage(page, 'sports', '#/sports', /Spor/);
      await page.locator('[data-page="measurements"]').click();
      await expectPage(page, 'measurements', '#/measurements', /Ölçümler/);

      // Clicking the open page again refreshes it without adding a history entry.
      const length = await page.evaluate(() => history.length);
      await page.locator('[data-page="measurements"]').click();
      assert.equal(await page.evaluate(() => history.length), length);

      await page.goBack();
      await expectPage(page, 'sports', '#/sports', /Spor/);
      await page.goBack();
      await expectPage(page, 'overview', '', /Genel bakış/);
      await page.goForward();
      await expectPage(page, 'sports', '#/sports', /Spor/);
      await until(async () => state.workouts >= 2, 'going forward loads the page data again');

      // A reload keeps the page.
      await page.reload();
      await page.waitForFunction(() => document.querySelectorAll('#history-rows tr').length === 7);
      await page.evaluate(() => { window.sameDocument = true; });
      await expectPage(page, 'sports', '#/sports', /Spor/);
      await page.close();

      // Deep link, and a dialog open on Back closes with the page it belonged to.
      const deep = await setup(browser, viewport, '/#/measurements');
      await expectPage(deep.page, 'measurements', '#/measurements', /Ölçümler/);
      await until(async () => deep.state.measurements === 1, 'a deep link loads its data once');
      await deep.page.locator('[data-page="system"]').click();
      await expectPage(deep.page, 'system', '#/system', /Sistem/);
      await deep.page.evaluate(() => document.getElementById('coverage-dialog').showModal());
      await deep.page.goBack();
      await expectPage(deep.page, 'measurements', '#/measurements', /Ölçümler/);
      assert.equal(await deep.page.evaluate(() => document.getElementById('coverage-dialog').open), false);
      await deep.page.close();

      // Fragments that name no page fall back to the overview and leave a clean URL. #sports is
      // the trap: it matches <section id="sports">, so treating it as a route would scroll.
      for (const hash of ['#/nonsense', '#sports']) {
        const odd = await setup(browser, viewport, `/${hash}`);
        await expectPage(odd.page, 'overview', '', /Genel bakış/);
        await odd.page.close();
      }

      // A fragment edited by hand is followed like Back.
      const typed = await setup(browser, viewport);
      await typed.page.evaluate(() => { location.hash = '#/system'; });
      await expectPage(typed.page, 'system', '#/system', /Sistem/);
      await typed.page.close();
      console.log(`PASS ${viewport.width}x${viewport.height}: URL, Back, Forward, reload, deep link, title, no document scroll`);
    }
  } finally {
    await browser.close();
  }
});
