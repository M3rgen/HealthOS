// Synthetic preview only; measure complete values and every text line, including scrolled rows.
const assert = require('node:assert/strict');
const fx = require('../../frontend/preview/fixtures.cjs');
const { BASE, launch, run, showCard, shot } = require('./lib.cjs');
const { mock, contained } = require('./helpers/accessibility.cjs');

run(async () => {
  const browser = await launch({ channel: 'chromium' });
  try {
    for (const [width, height] of [[320, 568], [390, 844]]) {
      const page = await browser.newPage({ viewport: { width, height }, reducedMotion: 'reduce' });
      await page.clock.setFixedTime(new Date(fx.NOW));
      await mock(page);
      await page.goto(BASE);
      await page.waitForFunction(() => document.querySelectorAll('#history-rows tr').length === 7 &&
        (innerWidth > 900 || document.querySelector('.overview-grid').hasAttribute('data-tabbed')));
      await page.evaluate(() => document.fonts.ready);
      await page.locator('.overview-tabs').waitFor({ state: 'visible' });
      await contained(page);
      await showCard(page, 'steps');
      await page.locator('.steps-card').waitFor({ state: 'visible' });
      await page.locator('.steps-card .metric-open').click();
      await page.locator('#metric-history-dialog[open]').waitFor();
      await page.locator('#metric-history-days li').last().waitFor();
      const expected = fx.history(new URLSearchParams({ metric: 'steps' })).days
        .map(day => day.value == null ? '—' : `${new Intl.NumberFormat('tr-TR').format(day.value)} adım`);
      assert.deepEqual(await page.locator('#metric-history-days strong').allTextContents(), expected);
      const failures = await page.locator('#metric-history-days li').evaluateAll(items => {
        const failures = [];
        const fits = (r, box) => r.left >= box.left - 1 && r.right <= box.right + 1 &&
          r.top >= box.top - 1 && r.bottom <= box.bottom + 1;
        for (const item of items) {
          for (const el of [item, ...item.querySelectorAll('*')]) {
            if (!el.getClientRects().length || getComputedStyle(el).visibility === 'hidden') continue;
            if (el.scrollWidth > el.clientWidth + 1)
              failures.push(`${el.textContent}: width ${el.scrollWidth}>${el.clientWidth}`);
          }
          const walker = document.createTreeWalker(item, NodeFilter.SHOW_TEXT);
          for (let node; (node = walker.nextNode());) {
            const parent = node.parentElement;
            if (!node.textContent.trim() || !parent.getClientRects().length ||
                getComputedStyle(parent).visibility === 'hidden') continue;
            const range = document.createRange();
            range.selectNodeContents(node);
            for (const rect of range.getClientRects()) {
              if (!fits(rect, item.getBoundingClientRect()) || !fits(rect, parent.getBoundingClientRect()))
                failures.push(`${node.textContent}: text line outside day or own cell`);
            }
          }
        }
        return failures;
      });
      await page.locator('#metric-history-days li').last().scrollIntoViewIfNeeded();
      await page.screenshot({ path: shot(`metric-history-cells-${width}x${height}.png`) });
      assert.deepEqual(failures, [], `${width}x${height}: ${JSON.stringify(failures)}`);
      await contained(page);
      await page.keyboard.press('Escape');
      await page.locator('#metric-history-dialog').waitFor({ state: 'hidden' });
      await contained(page);
      console.log(`PASS ${width}x${height}: history opens; full values, element widths and text lines fit; no page scroll`);
      await page.close();
    }
  } finally { await browser.close(); }
});
