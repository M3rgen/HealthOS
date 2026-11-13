// The Sistem panel says whether this archive has a backup, and the row keeps the page on one screen.
const assert = require('node:assert/strict');
const { BASE, launch, run } = require('./lib.cjs');
const fx = require('../../frontend/preview/fixtures.cjs');

const CASES = [
  ['backup_missing', null, 'Henüz yedek alınmadı'],
  ['backup_status_unreadable', null, 'Yedek durumu okunamadı'],
  ['backup_failed', fx.health.last_backup_success, 'Son deneme başarısız · son başarılı'],
  [null, fx.health.last_backup_success, null],
];

run(async () => {
  const browser = await launch();
  try {
    for (const [width, height] of [[1280, 800], [390, 844], [320, 640]]) {
      for (const [reason, success, text] of CASES) {
        const page = await browser.newPage({ viewport: { width, height } });
        await page.route('**/health', route => route.fulfill({
          json: { ...fx.health, reasons: reason ? [reason] : [], last_backup_success: success },
        }));
        await page.route('**/api/dashboard?**', route => route.fulfill({
          json: fx.dashboard(new URL(route.request().url()).searchParams),
        }));
        await page.goto(BASE);
        await page.locator('[data-page="system"]').click();
        await page.waitForFunction(() => document.getElementById('system-details').textContent.includes('Son yedek'));
        const details = await page.locator('#system-details').textContent();
        if (text) assert(details.includes(`Son yedek${text}`), details);
        else assert(!details.includes('Henüz yedek') && !details.includes('başarısız'), details);
        assert(await page.evaluate(() => {
          const root = document.scrollingElement;
          return root.scrollHeight <= innerHeight && root.scrollWidth <= innerWidth;
        }), `${width}x${height}: main page must fit (${reason})`);
        await page.close();
      }
      console.log(`PASS ${width}x${height}: backup row without page scrolling`);
    }
  } finally { await browser.close(); }
});
