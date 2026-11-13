// Synthetic fixtures only. The one-screen rule in the states a healthy dashboard never shows: a
// failed load, an empty archive and a notice. Neither the document, <main> nor the visible .page
// section may scroll, at the floor sizes and at the sizes where the overview once scrolled, and
// at the browser's 200 % font. A panel scrolls inside itself; the page does not.
const assert = require('node:assert/strict');
const fx = require('../../frontend/preview/fixtures.cjs');
const { BASE, launch, run } = require('./lib.cjs');

const FLOOR_SIZES = require('./viewport-floors.json').sizes.map(([width, height]) => [width, height]);
const LOW_LAPTOPS = [[1366, 650], [1280, 600], [1024, 600], [901, 600], [1280, 550]];
const LARGE_FONT_SIZES = [[1920, 1080], [1366, 768], [1024, 600], [844, 390], [390, 844], [320, 568], [320, 256]];
const failing = { status: 503, json: { detail: 'Sentetik hata' } };
const blank = fx.scenario('empty');
const SCENARIOS = {
  error: { '/api/dashboard': failing, '/api/dashboard/measurements': failing },
  empty: {
    '/health': { json: blank.health }, '/api/dashboard': blank.dashboard, '/api/dashboard/measurements': { json: [] },
    '/api/webhooks/status': { json: blank.webhookStatus }, '/sync/status': { json: blank.syncStatus },
    '/api/sync/coverage': blank.coverage, '/api/devices': { json: blank.devices }, '/api/dashboard/history': blank.history,
  },
};

async function open(browser, viewport, routes, font) {
  const page = await browser.newPage({ viewport, reducedMotion: 'reduce', hasTouch: viewport.width <= 900 });
  if (font) {
    const cdp = await page.context().newCDPSession(page);
    await cdp.send('Page.setFontSizes', { fontSizes: { standard: font, fixed: Math.round(font * 0.8125) } });
  }
  await page.clock.setFixedTime(new Date(fx.NOW));
  await page.route('**/*', route => {
    const url = new URL(route.request().url()), p = url.pathname;
    if (routes[p]) return typeof routes[p] === 'function' ? route.fulfill({ json: routes[p](url.searchParams) }) : route.fulfill(routes[p]);
    const json = body => route.fulfill({ json: typeof body === 'function' ? body(url.searchParams) : body });
    if (p === '/health') return json(fx.health);
    if (p === '/api/dashboard') return json(fx.dashboard);
    if (p === '/api/dashboard/history') return json(fx.history);
    if (p === '/api/dashboard/measurements') return json(fx.measurements);
    if (p === '/api/dashboard/workouts') return json(fx.workouts);
    if (p === '/api/sync/coverage') return json(fx.coverage);
    if (p === '/api/webhooks/status') return json(fx.webhookStatus);
    if (p === '/api/devices') return json(fx.devices);
    if (p === '/sync/status') return json(fx.syncStatus);
    if (p === '/api/settings/appearance') return json(fx.appearanceSettings);
    if (/^\/(api|sync|auth|google-health)\//.test(p)) return route.fulfill({ status: 404, json: {} });
    return route.continue();
  });
  await page.routeWebSocket(/\/api\/codex\/chat$/, () => {});
  await page.goto(BASE);
  return page;
}

const overflow = page => page.evaluate(() => {
  const over = (name, el, width = el.clientWidth, height = el.clientHeight) =>
    ({ name, x: el.scrollWidth - width, y: el.scrollHeight - height });
  return [over('document', document.scrollingElement, innerWidth, innerHeight), over('main', document.querySelector('main')),
    over('page', document.querySelector('.page:not([hidden])'))].filter(item => item.x > 1 || item.y > 1);
});

async function views(page) {
  const tabbed = await page.evaluate(() => document.querySelector('.overview-grid').hasAttribute('data-tabbed'));
  return [...(tabbed ? ['profile', 'connection', 'steps', 'sleep', 'heart'].map(key => `tab:${key}`) : ['overview']), 'measurements', 'system', 'sports'];
}
async function show(page, view) {
  if (view.startsWith('tab:')) {
    await page.evaluate(() => document.querySelector('[data-page="overview"]').click());
    await page.evaluate(key => document.querySelector(`#overview-tab-${key}`).click(), view.slice(4));
  } else {
    await page.evaluate(id => document.querySelector(`[data-page="${id}"]`).click(), view);
  }
  await page.waitForTimeout(60);
}

async function check(browser, { scenario, width, height, font, problems }) {
  const page = await open(browser, { width, height }, SCENARIOS[scenario], font);
  try {
    const kind = scenario === 'error' ? 'error' : 'empty';
    await page.waitForFunction(state => document.querySelector('#overview-state').dataset.state === state, kind);
    for (const view of await views(page)) {
      await show(page, view);
      for (const item of await overflow(page)) problems.push(`${scenario} ${width}x${height} font ${font ?? 16} ${view}: ${JSON.stringify(item)}`);
    }
  } finally { await page.close(); }
}

run(async () => {
  const browser = await launch();
  try {
    const jobs = [];
    for (const scenario of Object.keys(SCENARIOS)) {
      for (const [width, height] of [...FLOOR_SIZES, ...LOW_LAPTOPS]) jobs.push({ scenario, width, height, font: null });
      for (const [width, height] of LARGE_FONT_SIZES) jobs.push({ scenario, width, height, font: 32 });
    }
    const problems = [];
    await Promise.all(Array.from({ length: 4 }, async () => {
      for (let job = jobs.shift(); job; job = jobs.shift()) await check(browser, { ...job, problems });
    }));
    assert.deepEqual(problems, [], 'the page scrolls in a failed or empty state');
    console.log('PASS error and empty states: no scrolling document, main or page at the floors and at 200 %');

    // A notice alone is the smallest failure; on the shortest laptops it used to overflow by 6 px.
    for (const [width, height] of [[1280, 550], [1366, 650], [1024, 600]]) {
      const page = await open(browser, { width, height }, {}, null);
      await page.waitForFunction(() => document.querySelectorAll('#history-rows tr').length === 7);
      await page.evaluate(() => {
        document.getElementById('notice').hidden = false;
        document.getElementById('notice-text').textContent = 'Sentetik bildirim metni';
      });
      assert.deepEqual(await overflow(page), [], `${width}x${height} with a notice`);
      await page.close();
    }
    console.log('PASS a notice alone does not scroll the page');

    // Empty archive: the main action is on screen, not below the panel's fold, at each laptop size.
    for (const [width, height] of [[1366, 768], [1280, 720], [1024, 600], [1920, 1080]]) {
      const page = await open(browser, { width, height }, SCENARIOS.empty, null);
      await page.waitForFunction(() => document.querySelector('#overview-state').dataset.state === 'empty');
      const placed = await page.evaluate(() => {
        const button = document.getElementById('add-measurement').getBoundingClientRect();
        const panel = document.querySelector('.profile-panel').getBoundingClientRect();
        return { button: [button.top, button.bottom], panel: [panel.top, panel.bottom] };
      });
      assert.ok(placed.button[0] >= placed.panel[0] && placed.button[1] <= placed.panel[1] && placed.button[1] <= height,
        `empty ${width}x${height}: ${JSON.stringify(placed)}`);
      await page.close();
    }
    console.log('PASS empty archive: "Ölçüm ekle" stays on screen');
  } finally { await browser.close(); }
});
