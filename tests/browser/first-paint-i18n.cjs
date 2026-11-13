// First paint must contain the complete Turkish dictionary even when no script can run.
const assert = require('node:assert/strict');
const { BASE, launch, run } = require('./lib.cjs');

run(async () => {
  const { trPage } = await import('../../frontend/src/i18n/tr.js');
  const browser = await launch();
  try {
    for (const viewport of [{ width: 1280, height: 720 }, { width: 390, height: 844 }]) {
      const context = await browser.newContext({ viewport, javaScriptEnabled: false });
      const page = await context.newPage();
      await page.goto(BASE);
      assert.equal(await page.title(), trPage['document.title']);
      const marked = await page.evaluate(() => [...document.querySelectorAll('*')].flatMap(element =>
        [...element.attributes].filter(attribute => /^data-i18n(?:-(?:aria-label|title|placeholder|alt|content))?$/.test(attribute.name))
          .map(attribute => ({ key: attribute.value, value: attribute.name === 'data-i18n'
            ? [...element.childNodes].filter(node => node.nodeType === Node.TEXT_NODE).map(node => node.textContent).join('').trim()
            : element.getAttribute(attribute.name.slice('data-i18n-'.length)) }))));
      assert.ok(marked.length > 200, 'all static page text and attributes are checked');
      for (const { key, value } of marked) assert.equal(value, trPage[key], key);
      assert.doesNotMatch(await page.content(), /\{\{i18n:/);
      console.log(`PASS ${viewport.width}x${viewport.height}: ${marked.length} Turkish texts/attributes before JavaScript`);
      await context.close();
    }
  } finally { await browser.close(); }
});
