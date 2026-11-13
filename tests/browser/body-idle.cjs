// Synthetic fixture; count real WebGL draw calls while the figure rests, then wake it by resize.
const assert = require('node:assert/strict');
const fx = require('../../frontend/preview/fixtures.cjs');
const { BASE, launch, run } = require('./lib.cjs');
run(async () => {
  const browser = await launch();
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 800 }, reducedMotion: 'reduce' });
    await page.addInitScript(() => {
      window.figureDraws = 0;
      for (const type of [WebGLRenderingContext, WebGL2RenderingContext]) for (const name of ['drawArrays', 'drawElements', 'drawArraysInstanced', 'drawElementsInstanced']) {
        const draw = type.prototype[name];
        if (!draw) continue;
        type.prototype[name] = function (...args) {
          if (this.canvas.closest('#body-model')) window.figureDraws++;
          return draw.apply(this, args);
        };
      }
    });
    await page.route('**/*', route => {
      const url = new URL(route.request().url()), p = url.pathname;
      if (url.origin !== new URL(BASE).origin) return route.abort();
      if (p === '/health') return route.fulfill({ json: fx.health });
      if (p === '/api/dashboard') return route.fulfill({ json: fx.dashboard(url.searchParams) });
      if (/^\/(api|sync|auth|google-health)\//.test(p)) return route.fulfill({ status: 404, json: {} });
      return route.continue();
    });
    await page.routeWebSocket(/\/api\/codex\/chat$/, () => {});
    await page.goto(BASE);
    await page.waitForFunction(() => document.querySelector('#body-model[data-model-loaded]'));
    await page.waitForTimeout(1000);
    const before = await page.evaluate(() => window.figureDraws);
    assert.ok(before > 0, 'real model draws occurred before the idle measurement');
    await page.waitForTimeout(2000);
    assert.equal(await page.evaluate(() => window.figureDraws), before, 'zero draws during two idle seconds');
    await page.setViewportSize({ width: 1366, height: 800 });
    await page.waitForFunction(count => window.figureDraws > count, before);
    await page.waitForTimeout(300);
    const resized = await page.evaluate(() => window.figureDraws);
    await page.waitForTimeout(2000);
    assert.equal(await page.evaluate(() => window.figureDraws), resized, 'resize repaints, then rests again');
    console.log('PASS idle figure: zero WebGL draws for two seconds; resize wakes and releases it');
    await page.close();
  } finally { await browser.close(); }
});
