// Synthetic fixtures; no live account records.
// Umay streaming: plain text while an answer streams, painted at most once per frame; Markdown
// once it is final; broken frames are skipped without dropping the connection; answer links open
// in a new tab; the finished answer is summarised in a polite live region.
const assert = require('node:assert/strict');
const fx = require('../../frontend/preview/fixtures.cjs');
const { BASE, launch, run, shot } = require('./lib.cjs');
const { contained } = require('./helpers/accessibility.cjs');

run(async () => {
  const browser = await launch();
  try {
    for (const viewport of [{ width: 1280, height: 800 }, { width: 390, height: 844 }]) {
      const name = `${viewport.width}x${viewport.height}`;
      const page = await browser.newPage({ viewport });
      const warnings = [];
      page.on('console', message => { if (message.type() === 'warning') warnings.push(message.text()); });
      await page.route('**/*', route => {
        const url = new URL(route.request().url());
        if (url.pathname === '/health') return route.fulfill({ json: fx.health });
        if (url.pathname === '/api/dashboard') return route.fulfill({ json: fx.dashboard(url.searchParams) });
        if (url.pathname === '/sync/status') return route.fulfill({ json: fx.syncStatus });
        if (/^\/(api|sync|auth|google-health)\//.test(url.pathname)) return route.fulfill({ status: 404, json: {} });
        return route.continue();
      });
      await page.addInitScript(() => {
        const Original = window.WebSocket;
        window.WebSocket = class {
          constructor(url) {
            if (!url.endsWith('/api/codex/chat')) return new Original(url);
            window.umaySocket = this;
            this.readyState = 1;
            setTimeout(() => this.emit({ type: 'ready', model: 'synthetic', effort: 'medium' }), 10);
          }
          emit(event) { this.onmessage?.({ data: typeof event === 'string' ? event : JSON.stringify(event) }); }
          send() {}
          close() {}
        };
      });
      await page.goto(BASE);
      await page.waitForFunction(() => document.querySelectorAll('#history-rows tr').length === 7);
      assert.equal(await page.locator('#assistant-announcer').getAttribute('aria-live'), 'polite');
      await page.locator('#assistant-open').click();
      await page.locator('#assistant-status').getByText('Aktif', { exact: true }).waitFor();
      await page.locator('#assistant-input').fill('Uykum nasıl?');
      await page.locator('#assistant-send').click();

      // Many deltas in one task: one frame, so at most one paint of the answer.
      const burst = await page.evaluate(() => {
        let paints = 0;
        const observer = new MutationObserver(records => { paints += records.filter(r => r.target.classList?.contains('chat-content') || r.target.parentElement?.classList.contains('chat-content')).length ? 1 : 0; });
        observer.observe(document.getElementById('assistant-messages'), { childList: true, subtree: true, characterData: true });
        const parts = ['**Uyku', '** özeti\n\n', 'Bu hafta ', 'ortalama ', '7 sa 20 dk.'];
        for (let i = 0; i < 200; i++) window.umaySocket.emit({ type: 'delta', id: 'a1', text: i < parts.length ? parts[i] : '' });
        window.umaySocket.emit('{"type":"delta","id":"a1","text":"yarım');
        window.umaySocket.emit('not json');
        return new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(() => {
          observer.disconnect();
          const content = document.querySelector('.chat-assistant .chat-content');
          resolve({ paints, streaming: content.classList.contains('is-streaming'), text: content.textContent,
            strong: content.querySelectorAll('strong').length, error: document.getElementById('assistant-error').hidden,
            status: document.getElementById('assistant-status').textContent });
        })));
      });
      assert.ok(burst.paints <= 2, `200 deltas in one task painted ${burst.paints} times`);
      assert.equal(burst.streaming, true);
      assert.equal(burst.strong, 0, 'no Markdown while the answer streams');
      assert.equal(burst.text, '**Uyku** özeti\n\nBu hafta ortalama 7 sa 20 dk.');
      assert.equal(burst.error, true, 'a broken frame shows no error');
      assert.equal(burst.status, 'Yazıyor', 'the connection survives broken frames');
      assert.equal(warnings.filter(text => text.includes('unreadable chat frame')).length, 2);
      assert.ok(!warnings.some(text => text.includes('yarım')), 'frame text is never logged');
      await page.screenshot({ path: shot(`umay-streaming-${name}.png`) });

      const final = '**Uyku** özeti\n\nBu hafta ortalama 7 sa 20 dk. Ayrıntı için [uyku rehberi](/#/system).';
      await page.evaluate(final => {
        window.umaySocket.emit({ type: 'message', id: 'a1', text: final });
        window.umaySocket.emit({ type: 'done', status: 'completed' });
      }, final);
      const done = await page.evaluate(() => {
        const content = document.querySelector('.chat-assistant .chat-content');
        const link = content.querySelector('a');
        return { streaming: content.classList.contains('is-streaming'), strong: content.querySelector('strong')?.textContent,
          link: link && { target: link.target, rel: link.rel, href: link.getAttribute('href') },
          announced: document.getElementById('assistant-announcer').textContent };
      });
      assert.equal(done.streaming, false);
      assert.equal(done.strong, 'Uyku');
      assert.deepEqual(done.link, { target: '_blank', rel: 'noopener noreferrer', href: '/#/system' });
      assert.equal(done.announced, 'Umay yanıtladı: Uyku özeti Bu hafta ortalama 7 sa 20 dk. Ayrıntı için uyku rehberi.');

      // An answer that ends without its final message still becomes Markdown; a stop is announced.
      await page.locator('#assistant-input').fill('Nabzım?');
      await page.locator('#assistant-send').click();
      await page.evaluate(() => {
        window.umaySocket.emit({ type: 'delta', id: 'a2', text: '*Nabız* ' });
        window.umaySocket.emit({ type: 'done', status: 'interrupted' });
      });
      const stopped = await page.evaluate(() => ({
        em: document.querySelectorAll('.chat-assistant .chat-content')[1].querySelector('em')?.textContent,
        announced: document.getElementById('assistant-announcer').textContent,
      }));
      assert.deepEqual(stopped, { em: 'Nabız', announced: 'Umay yanıtı durduruldu.' });
      await page.screenshot({ path: shot(`umay-final-${name}.png`) });
      await contained(page);
      console.log(`PASS ${name}: ${burst.paints} paint(s) for 200 deltas, plain text while streaming, Markdown when final, broken frames skipped, links in a new tab, live summary`);
      await page.close();
    }
  } finally {
    await browser.close();
  }
});
