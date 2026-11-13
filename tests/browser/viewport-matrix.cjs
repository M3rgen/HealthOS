// Synthetic fixtures only; no live account records.
// The one-screen rule, measured: every page at every size down to 320×256 keeps the document
// from scrolling (without clipping it), the overview's key regions keep a useful visible size,
// the browser's font-size preference applies, touch targets reach 44 px, and text stays ≥ 12 px.
const assert = require('node:assert/strict');
const fx = require('../../frontend/preview/fixtures.cjs');
const { BASE, launch, run, shot } = require('./lib.cjs');

// The floors live in viewport-floors.json (width, height, min visible height of the selected/first
// metric card, of its chart, of the history panel, min fully visible history rows).
// tests/test_viewport_floors.py pins them: a floor cannot be lowered without changing that test.
const FLOORS = require('./viewport-floors.json');
const SIZES = FLOORS.sizes;
// Browser font-size preferences at which every control must stay whole and legible: 150 % and 200 %.
const LARGE_FONTS = [24, 32];
const FONT_VIEWPORTS = [[1920, 1080], [1366, 768], [1280, 720], [1024, 600], [844, 390], [390, 844], [375, 667], [320, 568], [320, 256]];

async function open(browser, viewport, routes = {}, font = null) {
  const page = await browser.newPage({ viewport, deviceScaleFactor: 1, reducedMotion: 'reduce',
    hasTouch: viewport.width <= 900 });
  if (font) {
    const cdp = await page.context().newCDPSession(page);
    await cdp.send('Page.setFontSizes', { fontSizes: { standard: font, fixed: Math.round(font * 0.8125) } });
  }
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.clock.setFixedTime(new Date(fx.NOW));
  await page.route('**/*', route => {
    const url = new URL(route.request().url()), p = url.pathname, q = url.searchParams;
    if (routes[p]) return typeof routes[p] === 'function' ? route.fulfill({ json: routes[p](q) }) : route.fulfill(routes[p]);
    const json = body => route.fulfill({ json: typeof body === 'function' ? body(q) : body });
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
  if (!Object.keys(routes).length) await page.waitForFunction(() => document.querySelectorAll('#history-rows tr').length === 7);
  await page.waitForTimeout(font ? 200 : 0);
  return { page, errors };
}

// Change the browser's font-size preference on an open page and wait for the rendering update that
// follows it. The stylesheet's `em` breakpoints move at once, but the page tracks them through a
// ResizeObserver (panel-tabs.js), and observers report only in the next rendering update. A
// GPU-less runner can hold that update back for seconds while page.evaluate still runs, so a fixed
// wait could measure a layout that is never painted. A 1rem box observed here reports the new size
// in that update, after every observer the page created earlier (they are notified in creation order).
async function changeFontSize(page, standard) {
  await page.evaluate(() => {
    const probe = document.createElement('span');
    probe.style.cssText = 'position:fixed;top:0;left:0;width:1rem;height:0;visibility:hidden;pointer-events:none';
    document.body.append(probe);
    const widths = [];
    const observer = new ResizeObserver(entries => widths.push(entries[0].contentRect.width));
    observer.observe(probe);
    window.__fontProbe = { widths, done: () => { observer.disconnect(); probe.remove(); } };
  });
  const cdp = await page.context().newCDPSession(page);
  await cdp.send('Page.setFontSizes', { fontSizes: { standard, fixed: Math.round(standard * 0.8125) } });
  await page.waitForFunction(size => window.__fontProbe.widths.includes(size), standard);
  await page.evaluate(() => { window.__fontProbe.done(); delete window.__fontProbe; });
}

const documentFits = page => page.evaluate(() => {
  const root = document.scrollingElement;
  const clipped = ['html', 'body'].map(tag => getComputedStyle(document.querySelector(tag)))
    .some(style => /hidden|clip/.test(style.overflowX + style.overflowY));
  // A page that scrolls inside <main> or inside its own .page section is still a scrolling page.
  const scrollers = [document.querySelector('main'), document.querySelector('.page:not([hidden])')].filter(Boolean)
    .map(el => ({ name: el.id || el.tagName, x: el.scrollWidth - el.clientWidth, y: el.scrollHeight - el.clientHeight }));
  return { w: innerWidth, h: innerHeight, sw: root.scrollWidth, sh: root.scrollHeight, clipped, scrollers };
});
async function assertFits(page, label) {
  const size = await documentFits(page);
  assert.ok(size.sw <= size.w && size.sh <= size.h, `${label}: document scrolls ${JSON.stringify(size)}`);
  assert.equal(size.clipped, false, `${label}: html/body clip the page instead of fitting it`);
  for (const s of size.scrollers)
    assert.ok(s.x <= 1 && s.y <= 1, `${label}: ${s.name} scrolls ${JSON.stringify(s)}`);
}

// Height of the element that is actually on screen: inside the viewport and every clipping ancestor.
const visibleHeight = (page, selector) => page.locator(selector).evaluate(element => {
  let rect = element.getBoundingClientRect();
  let top = Math.max(rect.top, 0), bottom = Math.min(rect.bottom, innerHeight);
  for (let parent = element.parentElement; parent; parent = parent.parentElement) {
    const style = getComputedStyle(parent);
    if (/auto|scroll|hidden|clip/.test(style.overflowY)) {
      rect = parent.getBoundingClientRect();
      top = Math.max(top, rect.top);
      bottom = Math.min(bottom, rect.bottom);
    }
  }
  return Math.max(0, Math.round(bottom - top));
});

const tabbed = page => page.evaluate(() => document.querySelector('.overview-grid').hasAttribute('data-tabbed'));
const allPages = ['measurements', 'system', 'sports'];

async function visibleViews(page) {
  const views = [];
  if (await tabbed(page)) for (const key of ['profile', 'connection', 'steps', 'sleep', 'heart']) views.push(`tab:${key}`);
  else views.push('overview');
  return [...views, ...allPages];
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

// Interactive elements on screen, their boxes, and the visible text sizes.
const audit = page => page.evaluate(() => {
  const visible = el => {
    const r = el.getBoundingClientRect(), style = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && style.visibility !== 'hidden' && style.display !== 'none' && !el.closest('[hidden]');
  };
  const small = [], targets = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const el = walker.currentNode.parentElement;
    if (!walker.currentNode.data.trim() || !visible(el) || el.closest('.sr-only, svg, canvas, [hidden]') || el.matches('.sr-only, option')) continue;
    const size = parseFloat(getComputedStyle(el).fontSize);
    if (size < 12) small.push(`${el.tagName.toLowerCase()}.${el.className}: ${size}`);
  }
  const page = document.querySelector('.page:not([hidden])');
  for (const el of document.querySelectorAll('button, a[href], select, input:not([type="hidden"]), summary')) {
    if (!visible(el) || el.closest('[hidden], .sr-only') || el.classList.contains('skip-link')) continue;
    if (el.closest('.page') && el.closest('.page') !== page) continue;
    // A checkbox inside its <label>: the label is the target, not the small box.
    const target = el.type === 'checkbox' && el.closest('label')?.querySelector('input') === el ? el.closest('label') : el;
    const r = target.getBoundingClientRect();
    targets.push({ name: el.id || el.className || el.tagName, w: Math.round(r.width * 10) / 10, h: Math.round(r.height * 10) / 10 });
  }
  return { small, targets };
});

// Controls and labels on screen: nothing cut off by a clipping ancestor, no two overlapping, no
// truncated label, no chart-axis labels running together. Rows that scroll sideways (navigation,
// tabs) may overflow along their axis; a box in a scrolling panel may run past the panel's edge.
const layoutAudit = page => page.evaluate(() => {
  const CTRL = 'a.brand, #header-date, .nav-button, .panel-tab, .history-handle, .button, #sync-button, #add-measurement, select, input:not([type=hidden])';
  const LABEL = 'h1, h2, h3, .status-text, .metric-top strong, .metric-note, .service-row > span, #queue-state, .chart-axis > *, .brand-wordmark, .connection-times dt, .connection-times dd';
  const ROWS = '.topbar > nav, .panel-tabs';
  const visible = el => {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 1 && r.height > 1 && s.visibility !== 'hidden' && s.display !== 'none' &&
      !el.closest('[hidden], .sr-only, .skip-link, dialog:not([open]), [popover]:not(:popover-open)') && !el.closest('.page')?.hidden;
  };
  const pick = sel => [...document.querySelectorAll(sel)].filter(el => visible(el) && !el.closest('#history-panel.expanded') && el.id !== 'assistant-open');
  const name = el => (el.id ? `#${el.id}` : `${el.tagName.toLowerCase()}.${String(el.className).split(/\s+/)[0]}`) + `:${el.textContent.trim().slice(0, 16)}`;
  const textBox = el => {
    const range = document.createRange();
    range.selectNodeContents(el);
    const rects = [...range.getClientRects()].filter(r => r.width > 0 && r.height > 0);
    if (!rects.length) { const r = el.getBoundingClientRect(); return { l: r.left, t: r.top, r: r.right, b: r.bottom }; }
    return { l: Math.min(...rects.map(r => r.left)), t: Math.min(...rects.map(r => r.top)), r: Math.max(...rects.map(r => r.right)), b: Math.max(...rects.map(r => r.bottom)) };
  };
  const onScreen = (el, box) => {
    let { l, t, r, b } = box;
    for (let p = el.parentElement; p; p = p.parentElement) {
      const s = getComputedStyle(p);
      if (/auto|scroll|hidden|clip/.test(s.overflowX + s.overflowY)) {
        const q = p.getBoundingClientRect();
        l = Math.max(l, q.left); t = Math.max(t, q.top); r = Math.min(r, q.right); b = Math.min(b, q.bottom);
      }
    }
    return { el, l, t, r, b };
  };
  const ctrls = pick(CTRL), labels = pick(LABEL);
  const clipped = [], overlap = [], truncated = [], runs = [];
  for (const el of [...ctrls, ...labels]) {
    const r = el.getBoundingClientRect();
    for (let p = el.parentElement; p; p = p.parentElement) {
      const s = getComputedStyle(p), q = p.getBoundingClientRect();
      const row = p.matches(ROWS);
      if ((/hidden|clip/.test(s.overflowY) || (row && /auto|scroll/.test(s.overflowY))) && (r.top < q.top - 1 || r.bottom > q.bottom + 1)) clipped.push(`${name(el)} cut by ${p.id || p.className || p.tagName} (y ${Math.round(r.top)}-${Math.round(r.bottom)} vs ${Math.round(q.top)}-${Math.round(q.bottom)})`);
      if (/hidden|clip/.test(s.overflowX) && (r.left < q.left - 1 || r.right > q.right + 1)) clipped.push(`${name(el)} cut by ${p.id || p.className || p.tagName} (x)`);
    }
    if (r.right > innerWidth + 1 && !el.closest('.topbar > nav, .panel-tabs, .table-scroll')) clipped.push(`${name(el)} past the right edge (${Math.round(r.right)})`);
    if (r.bottom > innerHeight + 1 && !el.closest('.panel, .connection, .metric-card, .page, .table-scroll')) clipped.push(`${name(el)} past the bottom edge (${Math.round(r.bottom)})`);
    const style = getComputedStyle(el);
    if ((style.textOverflow === 'ellipsis' || el.matches(CTRL)) && el.scrollWidth > el.clientWidth + 1 && !el.closest('.chart')) truncated.push(`${name(el)} ${el.scrollWidth}>${el.clientWidth}`);
  }
  const boxes = [...ctrls.map(el => { const r = el.getBoundingClientRect(); return onScreen(el, { l: r.left, t: r.top, r: r.right, b: r.bottom }); }),
    ...labels.map(el => onScreen(el, textBox(el)))].filter(v => v.r > v.l && v.b > v.t);
  const handle = v => v.el.id === 'history-toggle', heading = v => v.el.closest('.history-heading');
  const stickyFooter = v => v.el.closest('.connection-footer'), inConnection = v => v.el.closest('.connection');
  for (let i = 0; i < boxes.length; i++) for (let j = i + 1; j < boxes.length; j++) {
    const a = boxes[i], b = boxes[j];
    if (a.el.contains(b.el) || b.el.contains(a.el)) continue;
    // The handle is the background strip of the heading row, and the panel's sticky footer covers
    // its own scrolled content while that content scrolls; both are designed overlaps.
    if ((handle(a) && heading(b)) || (handle(b) && heading(a))) continue;
    if ((stickyFooter(a) && !stickyFooter(b) && inConnection(b)) || (stickyFooter(b) && !stickyFooter(a) && inConnection(a))) continue;
    const dx = Math.min(a.r, b.r) - Math.max(a.l, b.l), dy = Math.min(a.b, b.b) - Math.max(a.t, b.t);
    const textOnly = !a.el.matches(CTRL) && !b.el.matches(CTRL);
    if (dx > 2 && dy > (textOnly ? 6 : 2) && a.t < innerHeight && b.t < innerHeight) overlap.push(`${name(a.el)} x ${name(b.el)} (${Math.round(dx)}x${Math.round(dy)})`);
  }
  for (const axis of document.querySelectorAll('.chart-axis')) {
    if (!visible(axis)) continue;
    const labels2 = [...axis.children].filter(visible);
    for (let i = 1; i < labels2.length; i++) {
      const a = textBox(labels2[i - 1]), b = textBox(labels2[i]);
      if (Math.abs(a.t - b.t) < 4 && b.l - a.r < 4) runs.push(`${axis.textContent.trim().slice(0, 24)} (gap ${Math.round(b.l - a.r)})`);
    }
  }
  return { clipped: [...new Set(clipped)], overlap: [...new Set(overlap)], truncated: [...new Set(truncated)], runs: [...new Set(runs)] };
});

async function hideLauncherOverlap(page, label) {
  const bad = await page.evaluate(() => {
    const launcher = document.getElementById('assistant-open').getBoundingClientRect();
    const hit = el => {
      const r = el.getBoundingClientRect();
      return r.width && r.height && r.left < launcher.right && r.right > launcher.left && r.top < launcher.bottom && r.bottom > launcher.top;
    };
    return ['#export-button', '#sync-button', '#add-measurement', '#measurements-add', '#history-toggle', '.nav-button', '.overview-tab']
      .flatMap(selector => [...document.querySelectorAll(selector)]).filter(el => el.offsetParent && hit(el))
      .map(el => el.id || el.textContent.trim());
  });
  assert.deepEqual(bad, [], `${label}: the assistant launcher covers controls`);
}

run(async () => {
  const browser = await launch();
  try {
    for (const [width, height, cardMin, chartMin, historyMin, historyRows] of SIZES) {
      const name = `${width}x${height}`;
      const { page, errors } = await open(browser, { width, height });
      await assertFits(page, name);
      for (const view of await visibleViews(page)) {
        await show(page, view);
        await assertFits(page, `${name} ${view}`);
      }
      // Key regions on the overview have a useful visible size.
      const isTabbed = await tabbed(page);
      await show(page, isTabbed ? 'tab:steps' : 'overview');
      for (const selector of ['.steps-card', '.sleep-card', '.heart-card']) {
        if (isTabbed && selector !== '.steps-card') continue;
        const card = await visibleHeight(page, selector);
        assert.ok(card >= cardMin, `${name}: ${selector} visible ${card}px, wanted ≥ ${cardMin}`);
        if (chartMin) {
          const chart = await visibleHeight(page, `${selector} .chart`);
          assert.ok(chart >= chartMin, `${name}: ${selector} chart visible ${chart}px, wanted ≥ ${chartMin}`);
        }
      }
      if (isTabbed) {
        const cut = await page.locator('.overview-tab').evaluateAll(tabs =>
          tabs.filter(tab => tab.scrollWidth > tab.clientWidth + 1).map(tab => tab.textContent));
        assert.deepEqual(cut, [], `${name}: tab labels are cut off`);
        await show(page, 'tab:profile');
        assert.ok(await visibleHeight(page, '.profile-panel') >= Math.min(cardMin, 70));
        await show(page, 'tab:steps');
      }
      const history = await visibleHeight(page, '#history-panel');
      assert.ok(history >= historyMin, `${name}: history visible ${history}px, wanted ≥ ${historyMin}`);
      if (historyRows) {
        const rows = await page.evaluate(() => {
          const table = document.querySelector('#history-table').getBoundingClientRect();
          const head = document.querySelector('#history-table thead').getBoundingClientRect();
          return [...document.querySelectorAll('#history-rows tr')].filter(row => {
            const r = row.getBoundingClientRect();
            return r.top >= head.bottom - 1 && r.bottom <= table.bottom + 1;
          }).length;
        });
        assert.ok(rows >= historyRows, `${name}: ${rows} of 7 history rows fully visible, wanted ≥ ${historyRows}`);
      }
      // Regular desktop sizes need no inner scrolling at all.
      if (width > 900 && height >= 768) {
        const scrolled = await page.locator('.profile-panel, .connection, .metric-card').evaluateAll(panels =>
          panels.filter(panel => panel.scrollHeight > panel.clientHeight + 1)
            .map(panel => `${panel.className} ${panel.scrollHeight}/${panel.clientHeight}`));
        assert.deepEqual(scrolled, [], `${name}: panels overflow at a regular size`);
      }
      await hideLauncherOverlap(page, name);
      // The handle always opens the full history; the expanded sheet fits the viewport too.
      if (height >= 320) {
        await page.evaluate(() => document.getElementById('history-toggle').click());
        await page.waitForSelector('#history-panel.expanded');
        await assertFits(page, `${name} expanded history`);
        const table = await visibleHeight(page, '#history-table');
        assert.ok(table >= 60, `${name}: expanded history shows ${table}px of its table`);
        await page.evaluate(() => document.getElementById('history-toggle').click());
        await page.waitForFunction(() => !document.getElementById('history-panel').classList.contains('expanded'));
      }
      await page.screenshot({ path: shot(`viewport-${name}.png`) }).catch(() => {});
      assert.deepEqual(errors, []);
      console.log(`PASS ${name}: ${isTabbed ? 'tabs' : 'all cards'}, every page fits`);
      await page.close();
    }

    // No height cliff at 650 px: the compact layout continues into the regular one.
    const heights = [];
    for (const height of [649, 650]) {
      const { page } = await open(browser, { width: 1366, height });
      heights.push(await visibleHeight(page, '.steps-card'));
      await page.close();
    }
    assert.ok(Math.abs(heights[0] - heights[1]) <= 24, `metric cards jump at 650 px: ${heights.join(' / ')}`);
    assert.ok(heights[0] >= 120, `metric cards at 649 px: ${heights[0]}`);
    console.log(`PASS no cliff at 650: cards ${heights.join(' / ')} px`);

    // The browser's own font-size setting (not page zoom) reaches the type, and the page still fits.
    for (const [width, height] of [[1366, 768], [390, 844], [844, 390], [320, 256]]) {
      const { page, errors } = await open(browser, { width, height });
      await changeFontSize(page, 32);
      const root = await page.evaluate(() => parseFloat(getComputedStyle(document.documentElement).fontSize));
      assert.equal(root, 32, 'the 200 % browser preference reaches the root');
      for (const view of await visibleViews(page)) {
        await show(page, view);
        await assertFits(page, `${width}x${height} at 200% ${view}`);
      }
      // Content taller than its panel stays reachable: centring must not push the top out of reach.
      if (width === 1366) {
        await show(page, 'overview');
        const reachable = await page.locator('.profile-panel').evaluate(panel => {
          panel.scrollTop = 0;
          return panel.querySelector('.measurement').getBoundingClientRect().top >= panel.getBoundingClientRect().top - 1;
        });
        assert.equal(reachable, true, '200 %: the first profile value is scrolled out of reach');
      }
      const fine = await page.evaluate(() => [...document.querySelectorAll('.nav-button, h2, .fine-print')].slice(0, 3).map(el => parseFloat(getComputedStyle(el).fontSize)));
      assert.ok(fine.every(size => size >= 24), `200 %: text stayed small ${fine}`);
      assert.deepEqual(errors, []);
      console.log(`PASS ${width}x${height}: 200 % browser font fits, text scales (${fine.join('/')} px)`);
      await page.close();
    }

    // Larger browser fonts (150 %, 200 %): no control or label is cut off, overlaps another, truncates,
    // and axis labels never run together. The nav row must stay whole on its cross axis, too.
    for (const font of LARGE_FONTS) for (const [width, height] of FONT_VIEWPORTS) {
      const { page, errors } = await open(browser, { width, height }, {}, font);
      const views = await visibleViews(page);
      for (const view of views) {
        await show(page, view);
        const label = `${width}x${height} at ${font}px ${view}`;
        await assertFits(page, label);
        const result = await layoutAudit(page);
        assert.deepEqual(result.clipped, [], `${label}: controls cut off`);
        assert.deepEqual(result.overlap, [], `${label}: controls overlap`);
        assert.deepEqual(result.truncated, [], `${label}: labels truncate`);
        assert.deepEqual(result.runs, [], `${label}: chart axis labels run together`);
      }
      assert.deepEqual(errors, []);
      console.log(`PASS ${width}x${height} at ${font}px: controls whole, separate and legible`);
      await page.close();
    }
    // The default font must meet the same layout audit on the common sizes.
    for (const [width, height] of [[1366, 768], [390, 844], [320, 568], [844, 390], [1024, 600]]) {
      const { page } = await open(browser, { width, height });
      for (const view of await visibleViews(page)) {
        await show(page, view);
        const result = await layoutAudit(page);
        assert.deepEqual({ ...result, runs: [] }, { clipped: [], overlap: [], truncated: [], runs: [] }, `${width}x${height} ${view} at the default font`);
        assert.deepEqual(result.runs, [], `${width}x${height} ${view}: axis labels run together`);
      }
      await page.close();
    }
    console.log('PASS layout audit at the default font');

    // Landscape phones: the drag opens the side-by-side panel in proportion, and it stays where released.
    for (const [width, height] of [[844, 390], [640, 360]]) {
      const { page } = await open(browser, { width, height });
      const panel = () => page.locator('#history-panel').evaluate(el => {
        const r = el.getBoundingClientRect();
        return { w: Math.round(r.width), h: Math.round(r.height), left: Math.round(r.left) };
      });
      const before = await panel();
      const box = await page.locator('#history-toggle').boundingBox();
      const cx = box.x + box.width / 2, cy = box.y + box.height / 2;
      await page.mouse.move(cx, cy);
      await page.mouse.down();
      await page.mouse.move(cx - 60, cy, { steps: 6 });
      await page.waitForTimeout(80);
      const part = await panel();
      await page.mouse.move(cx - 120, cy, { steps: 6 });
      await page.waitForTimeout(80);
      const further = await panel();
      await page.mouse.up();
      await page.waitForTimeout(120);
      const released = await panel();
      assert.ok(Math.abs(part.w - before.w - 60) <= 4, `${width}x${height} drag of 60 px widened by ${part.w - before.w}`);
      assert.ok(Math.abs(further.w - before.w - 120) <= 4, `${width}x${height} drag of 120 px widened by ${further.w - before.w}`);
      assert.equal(released.w, further.w, 'the released width stays');
      await assertFits(page, `${width}x${height} after dragging the history`);
      // Dragged back to where it started, the panel closes again.
      const open2 = await page.locator('#history-toggle').boundingBox();
      await page.mouse.move(open2.x + open2.width / 2, open2.y + open2.height / 2);
      await page.mouse.down();
      await page.mouse.move(open2.x + open2.width / 2 + 200, open2.y + open2.height / 2, { steps: 6 });
      await page.mouse.up();
      await page.waitForFunction(() => !document.getElementById('history-panel').classList.contains('expanded'));
      await page.close();
    }
    console.log('PASS landscape phones: the handle opens the history in proportion to the drag');

    // First run: the next step is on the connection card, not below its visible area.
    {
      const sc = fx.scenario('empty');
      const routes = { '/health': { json: sc.health }, '/api/dashboard': q => sc.dashboard(q), '/api/dashboard/measurements': { json: [] },
        '/api/webhooks/status': { json: sc.webhookStatus }, '/sync/status': { json: sc.syncStatus }, '/api/sync/coverage': q => sc.coverage(q),
        '/api/devices': { json: sc.devices }, '/api/dashboard/history': q => sc.history(q) };
      for (const [width, height] of [[1280, 720], [1366, 768], [1024, 600]]) {
        const { page } = await open(browser, { width, height }, routes);
        await page.waitForTimeout(250);
        const placed = await page.evaluate(() => {
          const button = document.getElementById('sync-button').getBoundingClientRect();
          const card = document.querySelector('.connection').getBoundingClientRect();
          return { button: [Math.round(button.top), Math.round(button.bottom)], card: [Math.round(card.top), Math.round(card.bottom)] };
        });
        assert.ok(placed.button[0] >= placed.card[0] && placed.button[1] <= placed.card[1] + 1, `first run ${width}x${height}: ${JSON.stringify(placed)}`);
        await assertFits(page, `first run ${width}x${height}`);
        await page.close();
      }
      console.log('PASS first run: "Şimdi eşitle" stays on the card');
    }

    // A failed page offers one retry, and on phones it does not eat the screen.
    {
      const failing = { status: 503, json: { detail: 'Sentetik hata' } };
      for (const [width, height, maxBlock] of [[390, 844, 130], [320, 568, 150]]) {
        const { page } = await open(browser, { width, height }, { '/api/dashboard': failing });
        await page.waitForFunction(() => document.querySelector('#overview-state').dataset.state === 'error');
        const retries = await page.locator('button:visible', { hasText: 'Yeniden dene' }).count();
        assert.equal(retries, 1, `${width}x${height}: ${retries} retry controls`);
        const used = await page.evaluate(() => ['notice', 'overview-state'].reduce((sum, id) => {
          const el = document.getElementById(id);
          return sum + (el.hidden || !el.offsetParent ? 0 : el.getBoundingClientRect().height);
        }, 0));
        assert.ok(used <= maxBlock, `${width}x${height}: the error state takes ${used} px, wanted ≤ ${maxBlock}`);
        await page.close();
      }
      console.log('PASS error state: one retry control on phones');
    }

    // Phones: the assistant launcher sits on no measurement value at rest.
    for (const [width, height] of [[390, 844], [375, 667], [320, 568]]) {
      const { page } = await open(browser, { width, height });
      await show(page, 'measurements');
      const covered = await page.evaluate(() => {
        const l = document.getElementById('assistant-open').getBoundingClientRect();
        const table = document.querySelector('#measurements .table-scroll').getBoundingClientRect();
        return [...document.querySelectorAll('#measurement-rows td')].filter(cell => {
          const r = cell.getBoundingClientRect();
          const top = Math.max(r.top, table.top), bottom = Math.min(r.bottom, table.bottom);
          return r.width && bottom > top && r.left < l.right && r.right > l.left && top < l.bottom && bottom > l.top;
        }).map(cell => cell.textContent.trim());
      });
      assert.deepEqual(covered, [], `${width}x${height}: the launcher covers measurement values`);
      await page.close();
    }
    console.log('PASS launcher clear of the measurement values on phones');

    // Phones: at rest the launcher covers no text of the overview's history table.
    for (const [width, height] of [[390, 844], [375, 667], [320, 568], [844, 390]]) {
      const { page } = await open(browser, { width, height }, { '/api/dashboard': fx.dashboardWithSources });
      await page.waitForFunction(() => document.querySelectorAll('#history-rows tr').length >= 7);
      const covered = await page.evaluate(() => {
        const l = document.getElementById('assistant-open').getBoundingClientRect();
        const table = document.querySelector('#history-table').getBoundingClientRect();
        return [...document.querySelectorAll('#history-rows td')].filter(cell => {
          const range = document.createRange();
          range.selectNodeContents(cell);
          return [...range.getClientRects()].some(r => {
            const top = Math.max(r.top, table.top), bottom = Math.min(r.bottom, table.bottom);
            return r.width && bottom > top && r.left < l.right && r.right > l.left && top < l.bottom && bottom > l.top;
          });
        }).map(cell => cell.textContent.trim());
      });
      assert.deepEqual(covered, [], `${width}x${height}: the launcher covers history values at rest`);
      await page.close();
    }
    console.log('PASS launcher clear of the history table on phones');

    // Touch targets and minimum text size on phones.
    for (const [width, height] of [[390, 844], [375, 667], [844, 390]]) {
      const { page, errors } = await open(browser, { width, height });
      for (const view of await visibleViews(page)) {
        await show(page, view);
        const result = await audit(page);
        assert.deepEqual(result.small, [], `${width}x${height} ${view}: text under 12 px`);
        const smallTargets = result.targets.filter(target => target.h < 44 || target.w < 44);
        assert.deepEqual(smallTargets, [], `${width}x${height} ${view}: touch targets under 44 px on either side`);
      }
      assert.deepEqual(errors, []);
      await page.close();
    }
    {
      const { page } = await open(browser, { width: 1440, height: 900 });
      for (const view of await visibleViews(page)) {
        await show(page, view);
        const result = await audit(page);
        assert.deepEqual(result.small, [], `desktop ${view}: text under 12 px`);
      }
      await page.close();
    }
    console.log('PASS touch targets ≥ 44 px on phones, text ≥ 12 px everywhere checked');

    // Skip link: first Tab stop, visible when focused, moves focus into the content.
    {
      const { page } = await open(browser, { width: 1366, height: 768 });
      await page.keyboard.press('Tab');
      assert.equal(await page.evaluate(() => document.activeElement.className), 'skip-link');
      const box = await page.locator('.skip-link').boundingBox();
      assert.ok(box.y >= 0 && box.height >= 20, `skip link off screen: ${JSON.stringify(box)}`);
      await page.keyboard.press('Enter');
      await page.waitForFunction(() => document.activeElement.id === 'main-content');
      // The overview grid is not a keyboard stop on wide screens.
      assert.equal(await page.locator('.overview-grid').getAttribute('tabindex'), null);
      assert.equal(await page.evaluate(() => {
        const el = document.querySelector('.overview-grid');
        return el.scrollHeight > el.clientHeight + 1;
      }), false, 'the overview grid does not scroll by itself');
      console.log('PASS skip link reaches the content; overview grid is not a tab stop');
      await page.close();
    }

    // Contrast of the dark sleep bars.
    {
      const { page } = await open(browser, { width: 1366, height: 768 });
      const ratio = await page.evaluate(() => {
        const css = getComputedStyle(document.documentElement);
        const toLum = hex => {
          const c = hex.trim().replace('#', '');
          const channel = i => { const v = parseInt(c.slice(i, i + 2), 16) / 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
          return 0.2126 * channel(0) + 0.7152 * channel(2) + 0.0722 * channel(4);
        };
        const a = toLum(css.getPropertyValue('--sleep-dark')), b = toLum(css.getPropertyValue('--surface'));
        return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
      });
      assert.ok(ratio >= 3, `dark sleep bars ${ratio.toFixed(2)}:1`);
      await page.close();
      console.log(`PASS dark sleep bars ${ratio.toFixed(2)}:1 on the card surface`);
    }

    // Phones: Umay opens as a full-screen sheet; wider screens keep the anchored panel.
    {
      const { page } = await open(browser, { width: 390, height: 844 });
      await page.locator('#assistant-open').click();
      await page.locator('#assistant-dialog').waitFor();
      const box = await page.locator('#assistant-dialog').boundingBox();
      assert.ok(box.x <= 0.5 && box.y <= 0.5 && box.width >= 389 && box.height >= 843, `phone sheet ${JSON.stringify(box)}`);
      const covered = await page.evaluate(() => {
        const r = document.getElementById('assistant-open').getBoundingClientRect();
        return document.getElementById('assistant-dialog').contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2));
      });
      assert.equal(covered, true, 'the sheet covers the launcher');
      await assertFits(page, 'phone assistant');
      await page.locator('#assistant-close').click();
      assert.equal(await page.locator('#assistant-open').isVisible(), true);
      await page.close();
      const wide = await open(browser, { width: 1366, height: 768 });
      await wide.page.locator('#assistant-open').click();
      await wide.page.locator('#assistant-dialog').waitFor();
      const panel = await wide.page.locator('#assistant-dialog').boundingBox();
      assert.ok(panel.width < 700 && panel.x > 500, `desktop panel stays anchored ${JSON.stringify(panel)}`);
      await wide.page.close();
      console.log('PASS assistant: full-screen on phones, anchored on wide screens');
    }

    // Empty and error states instead of a bare table or a blank overview.
    const failing = { status: 503, json: { detail: 'Sentetik hata' } };
    const states = [
      ['Measurements empty', { '/api/dashboard/measurements': { json: [] } }, 'measurements', '#measurements-state', 'empty'],
      ['Measurements error', { '/api/dashboard/measurements': failing }, 'measurements', '#measurements-state', 'error'],
      ['Overview error', { '/api/dashboard': failing }, 'overview', '#overview-state', 'error'],
    ];
    for (const [label, routes, view, box, kind] of states) {
      for (const [width, height] of [[390, 844], [1366, 768], [320, 256]]) {
        const { page, errors } = await open(browser, { width, height }, routes);
        await page.evaluate(id => document.querySelector(`[data-page="${id}"]`).click(), view);
        await page.waitForFunction(({ box, kind }) => document.querySelector(box).dataset.state === kind, { box, kind });
        assert.equal(await page.locator(box).isVisible(), true, `${label} visible at ${width}x${height}`);
        assert.equal(await page.locator(`${box} strong`).isVisible(), true);
        if (kind === 'empty') {
          assert.equal(await page.locator('#measurements .table-scroll').isVisible(), false, 'the bare table gives way to the empty state');
          assert.equal(await page.locator(`${box}-action`).isVisible(), true);
        } else {
          // One retry control: the notice carries it, the state block does not repeat it.
          assert.equal(await page.locator('#notice .notice-action').isVisible(), true, 'the error offers a retry');
          assert.equal(await page.locator(`${box}-action`).isVisible(), false, 'no second retry control');
        }
        await assertFits(page, `${label} ${width}x${height}`);
        assert.deepEqual(errors.filter(error => !/503/.test(error)), []);
        await page.close();
      }
      console.log(`PASS ${label}`);
    }
    // Overview empty: every day carries no value.
    {
      const blank = params => {
        const data = fx.dashboard(params);
        return { ...data, days: data.days.map(day => ({ ...day, steps: null, sleep_seconds: null, resting_bpm: null, hrv_ms: null, spo2: null })) };
      };
      const page = await browser.newPage({ viewport: { width: 1366, height: 768 }, reducedMotion: 'reduce' });
      await page.clock.setFixedTime(new Date(fx.NOW));
      await page.route('**/*', route => {
        const url = new URL(route.request().url()), p = url.pathname;
        const json = body => route.fulfill({ json: body });
        if (p === '/health') return json(fx.health);
        if (p === '/api/dashboard') return json(blank(url.searchParams));
        if (p === '/api/settings/appearance') return json(fx.appearanceSettings);
        if (/^\/(api|sync|auth|google-health)\//.test(p)) return route.fulfill({ status: 404, json: {} });
        return route.continue();
      });
      await page.routeWebSocket(/\/api\/codex\/chat$/, () => {});
      await page.goto(BASE);
      await page.waitForFunction(() => document.querySelector('#overview-state').dataset.state === 'empty');
      await assertFits(page, 'Overview empty');
      await page.close();
      console.log('PASS Overview empty');
    }
  } finally { await browser.close(); }
});
