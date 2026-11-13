// Synthetic fixtures. A lost GPU context shows the silhouette instead of a blank figure, and a
// restored context brings the rendered model back.
const assert = require('node:assert/strict');
const fx = require('../../frontend/preview/fixtures.cjs');
const { BASE, launch, run, until } = require('./lib.cjs');

// The figure's screenshot as raw RGBA, decoded in the page.
async function picture(page) {
  const box = await page.locator('#body-model').boundingBox();
  const png = (await page.screenshot({ clip: box })).toString('base64');
  return page.evaluate(async data => {
    const image = new Image();
    image.src = `data:image/png;base64,${data}`;
    await image.decode();
    const canvas = document.createElement('canvas');
    canvas.width = image.width; canvas.height = image.height;
    const context = canvas.getContext('2d');
    context.drawImage(image, 0, 0);
    return [...context.getImageData(0, 0, canvas.width, canvas.height).data];
  }, png);
}
// Mean absolute difference per colour channel, 0–255.
function difference(a, b) {
  assert.equal(a.length, b.length);
  let sum = 0;
  for (let i = 0; i < a.length; i++) if (i % 4 !== 3) sum += Math.abs(a[i] - b[i]);
  return sum / (a.length * 3 / 4);
}

run(async () => {
  const browser = await launch();
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 800 }, reducedMotion: 'reduce' });
    const errors = []; page.on('pageerror', error => errors.push(error.message));
    await page.route('**/*', route => {
      const url = new URL(route.request().url());
      const p = url.pathname;
      if (p === '/health') return route.fulfill({ json: fx.health });
      if (p === '/api/dashboard') return route.fulfill({ json: fx.dashboard(url.searchParams) });
      if (/^\/(api|sync|auth|google-health)\//.test(p)) return route.fulfill({ status: 404, json: {} });
      return route.continue();
    });
    await page.routeWebSocket(/\/api\/codex\/chat$/, () => {});
    await page.goto(BASE);
    await page.waitForFunction(() => document.getElementById('body-model').dataset.modelLoaded === 'true', undefined, { timeout: 60000 });
    const silhouette = page.locator('#body-model .model-loading');
    const opacity = () => silhouette.evaluate(el => Number(getComputedStyle(el).opacity));
    await until(async () => (await opacity()) < .05, 'the silhouette fades once the model is shown', 5000);
    const before = await picture(page);
    const lose = await page.evaluate(() => {
      const canvas = document.querySelector('#body-model canvas');
      window.loseExtension = (canvas.getContext('webgl2') || canvas.getContext('webgl')).getExtension('WEBGL_lose_context');
      window.loseExtension.loseContext();
      return true;
    });
    assert.ok(lose);
    await page.waitForFunction(() => !('modelLoaded' in document.getElementById('body-model').dataset));
    await until(async () => (await opacity()) > .4, 'the silhouette must show during context loss', 5000);
    await page.waitForTimeout(600);
    const lost = await picture(page);
    assert.equal(await page.evaluate(() => document.getElementById('body-model').dataset.modelFailed), undefined);
    await page.evaluate(() => window.loseExtension.restoreContext());
    await page.waitForFunction(() => document.getElementById('body-model').dataset.modelLoaded === 'true', undefined, { timeout: 60000 });
    await until(async () => (await opacity()) < .05, 'the silhouette fades once the model is shown', 5000);
    // The model repainted exactly as before, lit by a rebuilt environment map: a black or blank
    // canvas would differ from the "before" frame as much as the silhouette does.
    await page.waitForTimeout(800);
    const restored = await picture(page);
    const toBefore = difference(before, restored), toLost = difference(before, lost);
    assert.ok(toLost > 2, `the silhouette frame must differ from the rendered figure (${toLost})`);
    assert.ok(toBefore < toLost / 3, `restored frame should match the original (${toBefore} vs ${toLost})`);
    assert.deepEqual(errors, []);
    console.log('PASS context loss shows the silhouette and a restored context repaints the model');
    await page.close();
  } finally { await browser.close(); }
});
