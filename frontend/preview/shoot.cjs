#!/usr/bin/env node
// Dev-only preview harness. Targets an already running Vite dev server, mocks
// every backend route with SYNTHETIC fixtures, screenshots each view at each
// viewport and fails when the main page can scroll in any direction.
//
//   node preview/shoot.cjs [--out .test-output/shoot] [--base http://127.0.0.1:5174]
//        [--viewports 1920x976,390x844] [--views overview,umay] [--reduced-motion]
//        [--languages tr,en] [--scale 2] [--readme]
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');
const fx = require('./fixtures.cjs');

const VIEWPORTS = require('../../tests/browser/viewport-floors.json').sizes.map(([w, h]) => `${w}x${h}`);
const LANGUAGES = ['tr', 'en'];
// The README images: English interface, 2x device pixels (1440x900 -> 2880x1800, 390x844 -> 780x1688).
const README = { viewports: ['1440x900', '390x844'], views: ['overview', 'umay', 'metric-history', 'calendar', 'appearance'],
  languages: ['en'], scale: 2 };
const VIEWS = {
  overview: async () => {},
  'history-drag': async (page) => {
    // Partial drag: the sheet must follow the pointer and keep its released height.
    const handle = await page.locator('#history-toggle').boundingBox();
    const x = handle.x + handle.width / 2, y = handle.y + handle.height / 2;
    const distance = Math.round(Math.min(220, Math.max(60, (y - 120) * 0.45)));
    await page.mouse.move(x, y); await page.mouse.down();
    await page.mouse.move(x, y - distance, { steps: 10 }); await page.mouse.up();
    await page.waitForTimeout(250);
  },
  'history-open': async (page) => {
    await page.locator('#history-toggle').click();
    await page.waitForFunction(() => document.querySelector('#history-panel.expanded') && document.querySelector('#history-status').hidden && document.querySelectorAll('#history-rows tr').length > 0);
  },
  'metric-history': async (page) => {
    // ≤900 px shows one overview card at a time (overview-tabs.js).
    if (await page.locator('#overview-tab-steps').isVisible()) await page.locator('#overview-tab-steps').click();
    await page.locator('.steps-card').click();
    await page.locator('#metric-history-days li').first().waitFor();
  },
  calendar: async (page) => {
    if (!(await page.locator('#header-date').isVisible())) return 'skip';
    await page.locator('#header-date').click();
    await page.locator('#date-calendar').waitFor();
  },
  'umay-intro': async (page) => {
    await page.locator('#assistant-open').click();
    await page.waitForFunction(() => document.querySelector('#assistant-status').dataset.presence === 'online');
    await page.evaluate(() => document.querySelector('.assistant-intro img').decode());
  },
  umay: async (page) => {
    await page.locator('#assistant-open').click();
    await page.waitForFunction(() => document.querySelector('#assistant-status').dataset.presence === 'online');
    await page.locator('#assistant-input').fill('Son haftamı özetler misin?');
    await page.locator('#assistant-send').click();
    await page.waitForFunction(() => document.querySelector('.chat-assistant') && document.querySelector('.chat-transfer') && !document.querySelector('#assistant-stop:not([hidden])'));
    return showTurn(page);
  },
  measurements: async (page) => {
    await page.locator('[data-page="measurements"]').click();
    await page.locator('#measurement-rows tr').first().waitFor();
  },
  'measurement-dialog': async (page) => {
    await page.locator('[data-page="measurements"]').click();
    await page.locator('#measurements-add').click();
    await page.locator('#measurement-dialog[open]').waitFor();
  },
  system: async (page) => {
    await page.locator('[data-page="system"]').click();
    await page.locator('#sync-rows tr').first().waitFor({ state: 'attached' });
  },
  sports: async (page) => {
    await page.locator('[data-page="sports"]').click();
    await page.locator('#workout-rows .workout-day').first().waitFor();
  },
  'sports-editor': async (page) => {
    await page.locator('[data-page="sports"]').click();
    await page.locator('#workout-rows .workout-day').first().waitFor();
    await page.locator('#workout-add').click();
  },
  appearance: async (page) => {
    await page.locator('[data-page="system"]').click();
    await page.waitForFunction(() => 'appearanceReady' in document.documentElement.dataset);
    if (await page.locator('#system-tab-settings').isVisible()) await page.locator('#system-tab-settings').click();
    await page.locator('#appearance-open').click();
    await page.locator('#appearance-dialog[open] .preset-tile').first().waitFor();
  },
  // Lane C presets applied, dialog closed: shows them on the real dashboard.
  'appearance-preset': async (page) => {
    await VIEWS.appearance(page);
    for (const [kind, id] of [['logo', 'dial'], ['umay', 'engraved'], ['background', 'kilim']]) {
      await page.locator(`input[name="appearance-${kind}"][value="${id}"]`).check();
    }
    await page.locator('#appearance-done').click();
    await page.locator('[data-page="overview"]').click();
    await page.waitForFunction(() => [...document.images].every(image => image.complete));
  },
  'appearance-uploaded': async (page) => {
    await VIEWS.appearance(page);
    const files = [['logo', 'logo.svg', 'image/svg+xml', fx.uploadLogoSvg()], ['umay', 'umay.png', 'image/png', fx.uploadAvatarPng()],
      ['background', 'zemin.png', 'image/png', fx.uploadBackgroundPng()]];
    for (const [kind, name, mimeType, buffer] of files) {
      await page.locator(`input[data-upload="${kind}"]`).setInputFiles({ name, mimeType, buffer });
      await page.locator(`input[name="appearance-${kind}"][value="upload"]:checked`).waitFor();
    }
    if (await page.evaluate(() => window.uploadedSvgRan || document.getElementById('uploaded-svg-marker'))) throw new Error('uploaded SVG reached the DOM');
  },
  // Edge cases with alternate fixtures (see scenario() in fixtures.cjs).
  notice: { scenario: 'sync-error', run: async (page) => { await page.locator('#notice:not([hidden])').waitFor(); } },
  'empty-overview': { scenario: 'empty', run: async () => {} },
  'empty-history': { scenario: 'empty', run: async (page) => {
    if (await page.locator('#overview-tab-steps').isVisible()) await page.locator('#overview-tab-steps').click();
    await page.locator('.steps-card').click();
    await page.locator('#metric-history-chart .history-empty').waitFor();
  } },
  'empty-system': { scenario: 'empty', run: async (page) => {
    await page.locator('[data-page="system"]').click();
    await page.locator('#google-empty:not([hidden])').waitFor();
  } },
  // No source selected: the empty overview offers the sample data, Sistem lists the three ways.
  'none-overview': { scenario: 'none', run: async (page) => { await page.locator('#overview-state-sample:not([hidden])').waitFor(); } },
  'none-system': { scenario: 'none', run: async (page) => {
    await page.locator('[data-page="system"]').click();
    await page.locator('#source-guide:not([hidden])').waitFor();
  } },
  // Sample data loaded: the banner on every page, the sample's label on its workouts.
  'sample-overview': { scenario: 'sample', run: async (page) => { await page.locator('#sample-banner:not([hidden])').waitFor(); } },
  'sample-sports': { scenario: 'sample', run: async (page) => {
    await page.locator('[data-page="sports"]').click();
    await page.locator('#sample-banner:not([hidden])').waitFor();
  } },
  // Two sources on the same day: source badges in the table and the opened "Diğer kaynaklar" sub-row.
  kaynaklar: { scenario: 'kaynaklar', run: async (page) => {
    await page.locator('#history-rows .source-badge').first().waitFor();
    await page.locator('#history-rows .source-toggle').first().click();
    await page.locator('#history-rows .source-alternatives:not([hidden])').first().waitFor();
  } },
  'empty-sports': { scenario: 'empty', run: async (page) => {
    await page.locator('[data-page="sports"]').click();
    await page.locator('.workouts-panel[data-empty]').waitFor();
  } },
};

// Umay's question, its transfer receipt and the answer in one frame, with the data notice above them
// either whole or scrolled away, never cut mid-sentence. The receipt stays folded: opened, its
// counts and dates need more height than the bubble has beside the answer. Returns a note when the
// bubble is too short for both (the notice then stays whole and the answer's end is below the fold).
async function showTurn(page) {
  return page.evaluate(() => {
    const log = document.getElementById('assistant-messages');
    if (log.scrollHeight <= log.clientHeight) { log.scrollTop = 0; return undefined; }
    const notice = document.getElementById('assistant-data-notice').getBoundingClientRect();
    const turn = document.querySelector('.chat-turn').getBoundingClientRect();
    // The question starts just below the log's top edge, between it and the notice's last line.
    const wanted = log.scrollTop + turn.top - log.getBoundingClientRect().top - Math.min(6, (turn.top - notice.bottom) / 2);
    if (wanted <= log.scrollHeight - log.clientHeight) { log.scrollTop = wanted; return undefined; }
    log.scrollTop = 0;
    const cut = Math.ceil(document.querySelector('.chat-assistant').getBoundingClientRect().bottom - log.getBoundingClientRect().bottom);
    return cut > 0 ? `Umay answer cut by ${cut} px below the whole data notice` : undefined;
  });
}

// How many pixels of a half-visible history row the sheet's edge cuts off (0 when every row on
// screen is whole). The overview's own layout decides this; the README preset reports it.
async function historyRowsCut(page) {
  return page.evaluate(() => {
    const box = document.querySelector('#overview .history .table-scroll').getBoundingClientRect();
    const rows = [...document.querySelectorAll('#history-rows tr')].map(row => row.getBoundingClientRect());
    const cut = rows.find(row => row.top < box.bottom - 1 && row.bottom > box.bottom + 1);
    return cut ? Math.ceil(cut.bottom - box.bottom) : 0;
  });
}

function args(argv = process.argv.slice(2)) {
  const out = { base: 'http://127.0.0.1:5174', viewports: VIEWPORTS, views: Object.keys(VIEWS), out: path.join(__dirname, '../.test-output/shoot'), reducedMotion: true,
    languages: LANGUAGES, scale: 1, readme: false };
  for (let i = 0; i < argv.length; i++) {
    const [key, inline] = argv[i].split('=');
    const value = () => inline ?? argv[++i];
    if (key === '--out') out.out = value();
    else if (key === '--base') out.base = value().replace(/\/$/, '');
    else if (key === '--viewports') out.viewports = value().split(',');
    else if (key === '--views') out.views = value().split(',');
    else if (key === '--reduced-motion') out.reducedMotion = true;
    else if (key === '--languages') out.languages = value().split(',');
    else if (key === '--scale') out.scale = Number(value());
    // README preset: its views, viewports, English and 2x; flags after it still override.
    else if (key === '--readme') Object.assign(out, README, { readme: true });
    else if (key === '--help' || key === '-h') { console.log(fs.readFileSync(__filename, 'utf8').split('\n').slice(1, 7).join('\n')); process.exit(0); }
    else throw new Error(`Unknown argument ${argv[i]}`);
  }
  const unknown = out.views.filter(v => !VIEWS[v]);
  if (unknown.length) throw new Error(`Unknown views: ${unknown.join(', ')} (known: ${Object.keys(VIEWS).join(', ')})`);
  const languages = out.languages.filter(language => !LANGUAGES.includes(language));
  if (languages.length || !out.languages.length) throw new Error(`Unknown languages: ${languages.join(', ')} (known: ${LANGUAGES.join(', ')})`);
  if (!(out.scale >= 1 && out.scale <= 3)) throw new Error(`--scale must be between 1 and 3, got ${out.scale}`);
  return out;
}

async function mock(page, base, problems, scenario) {
  const origin = new URL(base).origin;
  const data = { ...fx, ...fx.scenario(scenario) };
  const call = (value, q) => typeof value === 'function' ? value(q) : value;
  const json = (route, body, status = 200) => route.fulfill({ status, json: body });
  let appearance = data.appearanceSettings; // per page: a PUT is read back by the next GET
  await page.route('**/*', async (route) => {
    const url = new URL(route.request().url());
    const method = route.request().method();
    if (url.origin !== origin) { problems.push(`remote request blocked: ${url.href}`); return route.abort(); }
    const p = url.pathname, q = url.searchParams;
    if (p === '/health') return json(route, data.health);
    if (p === '/api/dashboard') return json(route, call(data.dashboard, q));
    if (p === '/api/dashboard/history') return json(route, call(data.history, q));
    if (p === '/api/dashboard/measurements') return method === 'POST'
      ? json(route, { id: 'new', ...route.request().postDataJSON(), source: 'Manuel' }, 201) : json(route, data.measurements);
    if (p === '/api/dashboard/workouts') return method === 'POST'
      ? json(route, { id: 'new', ...route.request().postDataJSON(), source: 'Manuel' }, 201) : json(route, call(data.workouts, q));
    if (p === '/api/dashboard/export.csv') return route.fulfill({ body: 'date,steps\n', contentType: 'text/csv' });
    if (p === '/api/settings/display') return json(route, { schema_version: 1, preferences: { timezone: 'UTC', motion: false }, updated_at: fx.NOW });
    if (p === '/api/settings/appearance') {
      if (method === 'PUT') appearance = { selection: route.request().postDataJSON(), updated_at: new Date().toISOString() };
      return json(route, appearance);
    }
    if (p === '/api/sync/coverage') return json(route, call(data.coverage, q));
    if (p === '/api/webhooks/status') return json(route, data.webhookStatus);
    if (p === '/api/devices') return json(route, data.devices);
    if (p === '/api/apple/status') return json(route, data.appleStatus);
    if (p === '/sync/status') return json(route, data.syncStatus);
    if (p === '/sync/all') return json(route, { status: 'accepted', data_types: ['steps'] }, 202);
    if (/^\/(api|sync|auth|docs|health|google-health)(\/|$)/.test(p)) { problems.push(`unmocked ${method} ${p}`); return json(route, { detail: 'preview: not mocked' }, 404); }
    return route.continue();
  });
  // Freeze Vite HMR: other edits to the tree must not reload a page mid-shot.
  await page.routeWebSocket((url) => url.searchParams.has('token') && url.pathname === '/', (ws) => {
    ws.send(JSON.stringify({ type: 'connected' }));
  });
  await page.routeWebSocket(/\/api\/codex\/chat$/, (ws) => {
    ws.send(JSON.stringify({ type: 'ready', model: 'önizleme', effort: 'medium' }));
    ws.onMessage((raw) => {
      const message = JSON.parse(String(raw));
      if (!message.text) return;
      ws.send(JSON.stringify({ type: 'working', text: 'Umay düşünüyor…' }));
      // The receipt arrives with the archive query, before the answer; it closes before 'done'.
      ws.send(JSON.stringify(fx.chatTransfer('active')));
      const parts = fx.chatAnswer.match(/[\s\S]{1,48}/g);
      parts.forEach((text, i) => setTimeout(() => ws.send(JSON.stringify({ type: 'delta', id: 'a1', text })), 80 + i * 15));
      setTimeout(() => ws.send(JSON.stringify(fx.chatTransfer('completed'))), 100 + parts.length * 15);
      setTimeout(() => ws.send(JSON.stringify({ type: 'done', status: 'completed' })), 120 + parts.length * 15);
    });
  });
}

async function measure(page) {
  return page.evaluate(() => {
    const s = document.scrollingElement;
    return { sh: s.scrollHeight, ih: innerHeight, sw: s.scrollWidth, iw: innerWidth };
  });
}

// Repeated captures must not depend on raster history. While the overview settles, the history
// table header moves in each of its first frames (~80 ms) as rows and columns fill in, and how far
// raster has got at each step differs between runs: on Linux the right part of the phone header
// ("Dinlenme nabzı" from x 270) sometimes kept its glyphs 1 px to the side of the settled raster
// (375x667 tr overview, 3 of 10 runs), with identical DOM and layout. Hiding the page for two
// frames and showing it again repaints every tile from the settled display list, and
// --disable-partial-raster makes each of those a full re-raster. The focused control stays
// visible, so focus, focus rings and focus events are left as they were.
async function repaintSettled(page) {
  await page.evaluate(async () => {
    const frames = (count) => new Promise((resolve) => {
      const step = () => (--count ? requestAnimationFrame(step) : resolve());
      requestAnimationFrame(step);
    });
    const style = document.createElement('style');
    style.textContent = 'html { visibility: hidden !important; } :focus { visibility: visible !important; }';
    document.head.append(style);
    await frames(2);
    style.remove();
    await frames(2);
  });
}

async function main() {
  const opt = args();
  fs.mkdirSync(opt.out, { recursive: true });
  // Partial raster re-draws only an invalidated part of a tile, and its anti-aliased seam can differ
  // by a few levels between runs (seen at the phone Steps card icon after #473). Full re-raster keeps
  // repeated captures byte-identical; differing content still shows up in tests/browser/preview-shoot.cjs.
  const browser = await chromium.launch({ headless: true, args: ['--disable-partial-raster'] });
  const rows = [];
  let failed = false;
  try {
    for (const viewport of opt.viewports) for (const language of opt.languages) {
      const [width, height] = viewport.split('x').map(Number);
      const contextOptions = { viewport: { width, height }, deviceScaleFactor: opt.scale,
        locale: language === 'en' ? 'en-US' : 'tr-TR', timezoneId: 'UTC', reducedMotion: opt.reducedMotion ? 'reduce' : 'no-preference',
        hasTouch: width <= 900, isMobile: false };
      for (const view of opt.views) {
        // A fresh context per view: stored appearance/uploads never leak between views.
        const context = await browser.newContext(contextOptions);
        await context.addInitScript(lang => {
          localStorage.setItem('healthos.language', lang);
          localStorage.setItem('healthos.timezone.v2', 'UTC');
          localStorage.setItem('healthos.motion', 'false');
        }, language);
        const page = await context.newPage();
        const problems = [];
        page.on('pageerror', (e) => problems.push(`pageerror: ${e.message}`));
        await page.clock.setFixedTime(new Date(fx.NOW));
        const spec = typeof VIEWS[view] === 'function' ? { run: VIEWS[view] } : VIEWS[view];
        await mock(page, opt.base, problems, spec.scenario);
        await page.goto(opt.base, { waitUntil: 'domcontentloaded' });
        await page.waitForFunction(() => document.querySelectorAll('#history-rows tr').length === 7);
        await page.evaluate(() => document.fonts.ready);
        // Narrow screens open on the Steps card and load the model only once Profile is shown (#473):
        // wait for it only where it is on screen, or every phone capture idles for the full 6 s.
        if (await page.locator('#body-model').isVisible()) await page.waitForSelector('#body-model[data-model-loaded]', { timeout: 6000 }).catch(() => {});
        await page.waitForTimeout(500); // let the body model fade in and settle
        let result;
        const notes = [];
        try {
          result = await spec.run(page);
          // Phones hide a row the sheet cannot show whole; the desktop table lets the edge cut it.
          const cut = opt.readme && view === 'overview' && !(await page.locator('#overview-tab-steps').isVisible()) ? await historyRowsCut(page) : 0;
          if (cut) notes.push(`last history row cut by ${cut} px at this height`);
          if (opt.readme && typeof result === 'string' && result !== 'skip') notes.push(result);
        } catch (e) { problems.push(`view step failed: ${e.message.split('\n')[0]}`); }
        const order = (opt.viewports.indexOf(viewport) * opt.languages.length + opt.languages.indexOf(language)) * opt.views.length + opt.views.indexOf(view);
        if (result === 'skip') { rows.push({ order, viewport, language, view, skipped: true }); await context.close(); continue; }
        await page.waitForFunction(() => document.getAnimations().every(a => a.playState !== 'running' || a.effect?.getComputedTiming().iterations === Infinity), null, { timeout: 2000 }).catch(() => {});
        await page.waitForTimeout(150);
        const m = await measure(page);
        const overflow = m.sh > m.ih || m.sw > m.iw;
        await repaintSettled(page);
        await page.screenshot({ animations: 'disabled', caret: 'hide', path: path.join(opt.out, `${viewport}-${language}-${view}.png`) });
        if (overflow || problems.length) failed = true;
        rows.push({ order, viewport, language, view, ...m, overflow, problems, notes });
        await context.close();
      }
    }
  } finally { await browser.close(); }

  rows.sort((a, b) => a.order - b.order);
  const pad = (s, n) => String(s).padEnd(n);
  console.log(`${pad('viewport', 11)}${pad('lang', 6)}${pad('view', 20)}${pad('scrollH/innerH', 16)}${pad('scrollW/innerW', 16)}result`);
  for (const r of rows) {
    if (r.skipped) { console.log(`${pad(r.viewport, 11)}${pad(r.language, 6)}${pad(r.view, 20)}${pad('-', 16)}${pad('-', 16)}skipped (not applicable)`); continue; }
    console.log(`${pad(r.viewport, 11)}${pad(r.language, 6)}${pad(r.view, 20)}${pad(`${r.sh}/${r.ih}`, 16)}${pad(`${r.sw}/${r.iw}`, 16)}${r.overflow ? 'OVERFLOW' : 'ok'}`);
    for (const p of r.problems) console.log(`    ! ${p}`);
    for (const note of r.notes) console.log(`    note: ${note}`);
  }
  console.log(`\nScreenshots: ${path.resolve(opt.out)}`);
  console.log(failed ? 'FAIL: page overflow or problems detected' : 'PASS: no main-page overflow in any view');
  process.exit(failed ? 1 : 0);
}

module.exports = { VIEWPORTS, LANGUAGES, README, args };
if (require.main === module) main().catch((error) => { console.error(error); process.exit(2); });
