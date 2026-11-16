// Workout detail view (Spor › "Rota ve grafikler"): the invented preview run around 0°/0°
// (frontend/preview/track-fixture.cjs) and, where a basemap is wanted, the synthetic PMTiles
// archive built in memory (frontend/preview/synthetic-tiles.cjs). No real place, track or tile.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const fx = require('../../frontend/preview/fixtures.cjs');
const { rangeAnswer, syntheticPmtiles } = require('../../frontend/preview/synthetic-tiles.cjs');
const { BASE, launch, run, shot, until } = require('./lib.cjs');
const { contained, mock, tabTo, visibleFocus } = require('./helpers/accessibility.cjs');
const { auditText: auditLayout } = require('./helpers/text-audit.cjs');
const { auditText: auditContrast, pixels } = require('./helpers/contrast.cjs');

const TRACK = fx.workoutTrack('w7');
const ARCHIVE = syntheticPmtiles();
// The policy the server sends with the page (app/core/security_headers.py), read from its source.
const POLICY = (() => {
  const source = fs.readFileSync(path.join(__dirname, '../../app/core/security_headers.py'), 'utf8');
  const block = source.slice(source.indexOf('APP_POLICY = ('), source.indexOf(')\n', source.indexOf('APP_POLICY = (')));
  return [...[...block.matchAll(/^\s+"([^"]+)",/gm)].map(match => match[1]), "connect-src 'self' blob:"].join('; ');
})();

/** @param {{tiles?: boolean, track?: object, status?: number[], webgl?: boolean, policy?: boolean}} remote */
async function open(browser, viewport, remote = {}, language = 'tr') {
  const page = await browser.newPage({ viewport, locale: language === 'en' ? 'en-US' : 'tr-TR', reducedMotion: 'reduce', timezoneId: 'UTC' });
  const seen = { errors: [], scripts: [], remote: [], violations: [], tiles: 0, tracks: 0 };
  page.on('pageerror', error => seen.errors.push(error.message));
  page.on('request', request => {
    const url = new URL(request.url());
    if (url.origin !== new URL(BASE).origin) seen.remote.push(url.origin);
    else if (/\.m?js$/.test(url.pathname)) seen.scripts.push(path.basename(url.pathname));
  });
  await page.addInitScript(([lang, webgl]) => {
    localStorage.setItem('healthos.language', lang);
    localStorage.setItem('healthos.timezone.v2', 'UTC');
    document.addEventListener('securitypolicyviolation', event => (window.violations ??= []).push(`${event.violatedDirective} ${event.blockedURI}`));
    if (!webgl) {
      const original = HTMLCanvasElement.prototype.getContext;
      HTMLCanvasElement.prototype.getContext = function (kind, ...rest) { return kind === 'webgl2' ? null : original.call(this, kind, ...rest); };
    }
  }, [language, remote.webgl !== false]);
  await page.clock.setFixedTime(new Date(fx.NOW));
  await mock(page);
  await page.route(/\/api\/dashboard\/workouts\?/, route => route.fulfill({ json: fx.workouts(new URL(route.request().url()).searchParams) }));
  await page.route(/\/api\/dashboard\/workouts\/[^/]+\/track/, route => {
    const status = remote.status?.[seen.tracks++] ?? 200;
    if (status !== 200) return route.fulfill({ status, json: { detail: 'Synthetic' } });
    return route.fulfill({ json: remote.track ?? TRACK });
  });
  await page.route('**/api/map/tiles.pmtiles', route => {
    seen.tiles++;
    if (!remote.tiles) return route.fulfill({ status: 404, json: { detail: 'Harita altlığı kurulu değil / No basemap installed' } });
    const answer = rangeAnswer(ARCHIVE, route.request().headers().range);
    return route.fulfill({ status: answer.status, headers: answer.headers, body: answer.body });
  });
  if (remote.policy) await page.route(url => url.pathname === '/', async route => {
    const response = await route.fetch();
    route.fulfill({ response, headers: { ...response.headers(), 'content-security-policy': POLICY } });
  });
  await page.goto(BASE);
  await page.waitForFunction(() => document.documentElement.dataset.booted === 'true');
  await page.locator('[data-page="sports"]').click();
  await page.locator('[data-workout-id="w7"] [data-track-open]').waitFor();
  return { page, seen };
}

async function openDetail(page) {
  await page.locator('[data-workout-id="w7"] [data-track-open]').click();
  await page.locator('.workout-chart svg').first().waitFor();
}

const chunk = (seen, name) => seen.scripts.some(file => file.startsWith(`${name}-`) || file === `${name}.js`);
const series = page => page.locator('.workout-chart').evaluateAll(nodes => nodes.map(node => node.dataset.series));
const cursorAt = async page => Number(await page.locator('.workout-charts').getAttribute('aria-valuenow'));

async function audit(page, label) {
  await page.evaluate(() => document.fonts.ready);
  await contained(page);
  const layout = await page.evaluate(auditLayout);
  assert.deepEqual(layout.overflow, [], `${label}: all text remains reachable`);
  assert.deepEqual(layout.overlap, [], `${label}: no text collisions`);
  const wide = await page.locator('.workout-detail').evaluate(root => [...root.querySelectorAll('*')]
    .filter(node => node.scrollWidth > node.clientWidth + 1 && !['svg', 'canvas'].includes(node.localName) && getComputedStyle(node).overflowX !== 'visible')
    .map(node => node.className));
  assert.deepEqual(wide.filter(name => !String(name).includes('table-scroll')), [], `${label}: nothing scrolls sideways`);
}

// Tiles absent: the route is drawn on its own, with the note; MapLibre is never downloaded.
async function fallback(browser, viewport, language) {
  const { page, seen } = await open(browser, viewport, {}, language);
  assert.equal(await page.locator('[data-track-open]').count(), 1, 'only the row with a track has the button');
  assert.equal(chunk(seen, 'workout-detail'), false, 'the detail view is not loaded before it is opened');
  await tabTo(page, '[data-workout-id="w7"] [data-track-open]');
  await page.keyboard.press('Enter');
  await page.locator('.workout-chart svg').first().waitFor();
  assert.equal(await page.evaluate(() => document.activeElement.classList.contains('workout-detail-back')), true);
  await visibleFocus(page);
  await page.locator('.workout-detail-map[data-map="route"] .workout-map-note:not(:empty)').waitFor();
  assert.match(await page.locator('.workout-map-note').textContent(), language === 'en' ? /No basemap/ : /altlığı kurulu değil/);
  assert.equal(await page.locator('.workout-route-line').count(), 2, 'the line is cut at the missing positions');
  assert.deepEqual(await series(page), ['pace', 'heart_rate', 'altitude'], 'no chart for channels the file lacks');
  assert.equal(chunk(seen, 'workout-map') || chunk(seen, 'maplibre-gl-worker'), false, 'no MapLibre without a basemap');
  assert.equal(await page.locator('.workout-laps tbody tr').count(), TRACK.track.laps.start_index.length);
  await audit(page, `${viewport.width} ${language} fallback`);
  return { page, seen };
}

// Chart and route share one cursor; the keyboard moves it along the range under the charts.
async function cursor(page) {
  await page.locator('.workout-chart-plot').first().scrollIntoViewIfNeeded();
  const plot = await page.locator('.workout-chart-plot').first().boundingBox();
  await page.mouse.move(plot.x + plot.width / 2, plot.y + plot.height / 2);
  await page.waitForFunction(() => document.querySelector('.workout-route-cursor')?.getAttribute('visibility') === 'visible');
  const hovered = await cursorAt(page);
  assert.ok(hovered > 100 && hovered < 600, `chart middle is mid-workout: ${hovered}`);
  const lap = TRACK.track.laps.start_index[1];
  await page.locator('.workout-route-lap').first().scrollIntoViewIfNeeded();
  const mark = await page.locator('.workout-route-lap').first().boundingBox();
  await page.mouse.click(mark.x + mark.width / 2, mark.y + mark.height / 2);
  assert.equal(await cursorAt(page), lap, 'a tap on the route moves the charts to that sample');
  assert.match(await page.locator('.workout-chart-value').first().textContent(), /\d:\d\d/);
  await tabTo(page, '.workout-charts');
  await page.keyboard.press('ArrowRight');
  assert.equal(await cursorAt(page), lap + Math.round(TRACK.track.point_count / 100));
  assert.match(await page.locator('.workout-charts').getAttribute('aria-valuetext'), /·/);
  await visibleFocus(page);
}

async function toggles(page) {
  await page.getByRole('button', { name: /Mesafe|Distance/ }).click();
  assert.match(await page.locator('.workout-chart-axis text').last().textContent(), /^\d+ km$/);
  await page.getByRole('button', { name: /^(Hız|Speed)$/ }).click();
  assert.deepEqual((await series(page))[0], 'speed');
  assert.match(await page.locator('.workout-detail-summary').textContent(), /km\/(sa|h)/);
  await page.getByRole('button', { name: /^(Tempo|Pace)$/ }).click();
  assert.deepEqual((await series(page))[0], 'pace');
}

async function close(page) {
  await page.locator('.workout-detail-back').focus();
  await page.keyboard.press('Escape');
  await page.locator('.workout-detail').waitFor({ state: 'detached' });
  assert.equal(await page.locator('.workouts-panel').isVisible(), true);
  assert.equal(await page.evaluate(() => document.activeElement.matches('[data-workout-id="w7"] [data-track-open]')), true);
}

// The synthetic basemap under the app's own CSP: map, attribution, and a tap on the start marker.
async function basemap(browser) {
  const { page, seen } = await open(browser, { width: 1440, height: 900 }, { tiles: true, policy: true });
  if (!(await page.evaluate(() => Boolean(document.createElement('canvas').getContext('webgl2'))))) {
    console.log('INFO no WebGL2 in this browser: the basemap path is covered by noWebgl only');
    return page.close();
  }
  await openDetail(page);
  await page.locator('.workout-detail-map[data-map-ready="true"]').waitFor({ timeout: 30000 });
  assert.equal(chunk(seen, 'maplibre-gl-worker'), true, 'the worker comes from this origin');
  assert.match(await page.locator('.workout-map-credit').textContent(), /OpenStreetMap/);
  assert.match(await page.locator('.workout-map-credit').textContent(), /^Protomaps © OpenStreetMap/);
  assert.deepEqual(await page.locator('.workout-map-credit a').evaluateAll(links => links.map(link => link.href)),
    ['https://protomaps.com/', 'https://www.openstreetmap.org/copyright']);
  assert.equal(await page.locator('.workout-map-credit').isVisible(), true);
  assert.deepEqual(await page.evaluate(() => window.violations ?? []), [], 'no CSP violation');
  const box = await page.locator('.workout-map-gl').boundingBox();
  const image = pixels(await page.screenshot({ clip: box }));
  const start = [];
  for (let y = 0; y < Math.floor(box.height); y += 1) for (let x = 0; x < Math.floor(box.width); x += 1) {
    const [r, g, b] = image(x, y);
    if (Math.abs(r - 0x48) < 12 && Math.abs(g - 0xd8) < 12 && Math.abs(b - 0x8c) < 12) start.push([x, y]);
  }
  assert.ok(start.length > 10, 'the start marker is drawn');
  const [sx, sy] = start.reduce(([ax, ay], [x, y]) => [ax + x / start.length, ay + y / start.length], [0, 0]);
  await page.mouse.click(box.x + sx, box.y + sy);
  await until(async () => await cursorAt(page) === 0 &&
    (await page.locator('.workout-chart-value').first().textContent()) !== '', 'a tap on the map moves the charts');
  await page.locator('.workout-map-tool').first().click();
  await page.screenshot({ path: shot('workout-detail-basemap.png') });
  assert.deepEqual(seen.errors, []);
  await page.close();
}

async function edgeCases(browser) {
  const narrow = { width: 390, height: 844 };
  const noWebgl = await open(browser, narrow, { tiles: true, webgl: false });
  await openDetail(noWebgl.page);
  await noWebgl.page.locator('.workout-map-note:not(:empty)').waitFor();
  assert.match(await noWebgl.page.locator('.workout-map-note').textContent(), /WebGL2/);
  assert.equal(noWebgl.seen.tiles, 0, 'no basemap request without WebGL2');
  assert.equal(chunk(noWebgl.seen, 'workout-map'), false);
  await noWebgl.page.close();
  const { samples } = TRACK.track;
  const indoor = { track: { ...TRACK.track, summary: { ...TRACK.track.summary, bounds: null },
    samples: Object.fromEntries(Object.entries(samples).filter(([key]) => !['latitude', 'longitude'].includes(key))) } };
  const inside = await open(browser, narrow, { tiles: true, track: indoor });
  await openDetail(inside.page);
  assert.equal(await inside.page.locator('.workout-detail-map[data-map="none"]').count(), 1);
  assert.equal(inside.seen.tiles, 0);
  await audit(inside.page, 'indoor');
  await inside.page.close();
  const failing = await open(browser, narrow, { track: { track: null }, status: [504] });
  await failing.page.locator('[data-workout-id="w7"] [data-track-open]').click();
  await failing.page.locator('.workout-detail-retry:not([hidden])').click();
  await failing.page.locator('.workout-detail-status', { hasText: /izi yok/ }).waitFor();
  await failing.page.close();
  const gone = await open(browser, narrow, { status: [404] });
  await gone.page.locator('[data-workout-id="w7"] [data-track-open]').click();
  await gone.page.locator('#workout-status', { hasText: /artık listede yok/ }).waitFor();
  assert.equal(await gone.page.locator('.workout-detail').count(), 0);
  await gone.page.close();
}

run(async () => {
  const browser = await launch();
  try {
    for (const [viewport, language] of [[{ width: 1440, height: 900 }, 'tr'], [{ width: 390, height: 844 }, 'en'], [{ width: 320, height: 700 }, 'tr']]) {
      const { page, seen } = await fallback(browser, viewport, language);
      if (viewport.width < 900) {
        const [map, side] = await Promise.all(['.workout-detail-map', '.workout-detail-side'].map(selector => page.locator(selector).boundingBox()));
        assert.ok(map.y + map.height <= side.y + 1, 'narrow screens stack the map above the charts');
      }
      await cursor(page);
      await toggles(page);
      await auditContrast(page, `workout detail ${viewport.width}`);
      await page.screenshot({ path: shot(`workout-detail-${viewport.width}-${language}.png`) });
      await close(page);
      assert.deepEqual([seen.errors, seen.remote], [[], []], 'no page error and no request to another origin');
      console.log(`PASS workout detail without a basemap ${viewport.width}x${viewport.height} ${language}`);
      await page.close();
    }
    await edgeCases(browser);
    console.log('PASS no WebGL2, indoor track, retry after 504, workout gone (404)');
    await basemap(browser);
    console.log('PASS synthetic basemap under the app CSP');
  } finally { await browser.close(); }
});
