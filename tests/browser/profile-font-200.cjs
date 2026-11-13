// UX-02: real browser font preference, synthetic overview, visible text intersections > 4 px².
const assert = require('node:assert/strict');
const { BASE, launch, run, showCard } = require('./lib.cjs');
const { mock, contained } = require('./helpers/accessibility.cjs');
const fx = require('../../frontend/preview/fixtures.cjs');

function intersections() {
  const boxes = [], overlaps = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const node = walker.currentNode, el = node.parentElement;
    if (!node.data.trim() || el.closest('script, style, svg, option, .sr-only, .skip-link:not(:focus), [hidden], dialog:not([open])')) continue;
    if (getComputedStyle(el).visibility !== 'visible') continue;
    const range = document.createRange();
    range.selectNode(node);
    for (const rect of range.getClientRects()) {
      let left = Math.max(0, rect.left), right = Math.min(innerWidth, rect.right);
      let top = Math.max(0, rect.top), bottom = Math.min(innerHeight, rect.bottom);
      for (let parent = el; parent; parent = parent.parentElement) {
        const style = getComputedStyle(parent), box = parent.getBoundingClientRect();
        if (style.opacity === '0' || style.clipPath === 'inset(50%)') right = left;
        if (/auto|scroll|hidden|clip/.test(style.overflowX)) {
          left = Math.max(left, box.left + parent.clientLeft);
          right = Math.min(right, box.left + parent.clientLeft + parent.clientWidth);
        }
        if (/auto|scroll|hidden|clip/.test(style.overflowY)) {
          top = Math.max(top, box.top + parent.clientTop);
          bottom = Math.min(bottom, box.top + parent.clientTop + parent.clientHeight);
        }
      }
      if (right > left && bottom > top) boxes.push({ el, text: node.data.trim(), left, right, top, bottom });
    }
  }
  for (let i = 0; i < boxes.length; i++) for (let j = i + 1; j < boxes.length; j++) {
    const a = boxes[i], b = boxes[j];
    if (a.el.contains(b.el) || b.el.contains(a.el)) continue;
    const width = Math.min(a.right, b.right) - Math.max(a.left, b.left);
    const height = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
    if (width > 0 && height > 0 && width * height > 4) overlaps.push([a.text, b.text, width * height]);
  }
  return { count: boxes.length, overlaps };
}

run(async () => {
  const browser = await launch();
  const failures = [];
  try {
    for (const [width, height] of [[320, 480], [320, 568], [390, 844]]) {
      const page = await browser.newPage({ viewport: { width, height }, reducedMotion: 'reduce' });
      try {
        const cdp = await page.context().newCDPSession(page);
        await cdp.send('Page.setFontSizes', { fontSizes: { standard: 32, fixed: 26 } });
        await page.clock.setFixedTime(new Date(fx.NOW));
        await mock(page);
        await page.route('**/api/dashboard?*', route => route.fulfill({ json: fx.dashboard(new URL(route.request().url()).searchParams) }));
        await page.goto(BASE);
        await page.waitForFunction(() => document.querySelectorAll('#history-rows tr').length === 7 &&
          document.querySelector('.overview-grid').hasAttribute('data-tabbed'));
        // Narrow screens open on the Steps card (#473); this check measures the Profile panel.
        await showCard(page, 'profile');
        await page.waitForFunction(() => document.querySelector('#overview-tab-profile')?.getAttribute('aria-selected') === 'true' &&
          !document.querySelector('.profile-panel').hidden);
        await page.evaluate(() => document.fonts.ready);
        assert.equal(await page.evaluate(() => parseFloat(getComputedStyle(document.documentElement).fontSize)), 32);
        await contained(page);
        const result = await page.evaluate(intersections);
        assert.ok(result.count > 10, 'the overview must contain visible text');
        if (result.overlaps.length) failures.push({ width, height, ...result });
        console.log(`INFO ${width}x${height}: ${JSON.stringify(result)}`);
      } finally { await page.close(); }
    }
  } finally { await browser.close(); }
  assert.deepEqual(failures, []);
  console.log('PASS 200% browser font: no visible text intersections > 4 px²; no page scrolling');
});
