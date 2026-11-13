// Apple Sağlık içe aktar on the System page: the panel, an upload with upload and server
// progress, the final summary, error answers, the receiver line, keyboard access and page
// containment at four viewport sizes. Synthetic fixtures only; no live account records.
const assert = require('node:assert/strict');
const path = require('node:path');
const { BASE, launch, run, until, shot } = require('./lib.cjs');
const fx = require(path.join(__dirname, '../../frontend/preview/fixtures.cjs'));

const sizes = [{width:1440,height:900}, {width:1280,height:720},
  {width:390,height:844}, {width:320,height:700}];
const contained = async page => assert.deepEqual(await page.evaluate(() => ({
  x:document.documentElement.scrollWidth > innerWidth,
  y:document.documentElement.scrollHeight > innerHeight,
  bodyX:document.body.scrollWidth > innerWidth, bodyY:document.body.scrollHeight > innerHeight,
})), {x:false,y:false,bodyX:false,bodyY:false});
const inside = async (page, selector, viewport) => {
  const box = await page.locator(selector).boundingBox();
  assert.ok(box && box.x >= 0 && box.x + box.width <= viewport.width + 1,
    `${selector} leaves the viewport horizontally: ${JSON.stringify(box)}`);
};

const ZIP = Buffer.concat([Buffer.from('PK\x03\x04'), Buffer.alloc(3 * 1024 * 1024, 7)]);
const DONE = {id:7, channel:'export', origin:'upload', status:'done', filename:'export.zip',
  started_at:'2026-10-16T14:00:00Z', finished_at:'2026-10-16T14:02:00Z', bytes_read:ZIP.length,
  records_read:123456, records_new:120000, rows_written:98000, export_date:'2026-10-15T08:00:00Z', error:null,
  summary:{format:'zip', member:'apple_health_export/export.xml', skipped:{Me:1, ClinicalRecord:3}}};

function status(state) {
  return {
    receiver:{enabled:state.receiver, token_file_present:state.receiver, endpoint_configured:false, path:'/webhooks/apple-health'},
    running:state.running ? {import_id:7, origin:'upload', filename:'export.zip', started_at:'2026-10-16T14:00:00Z',
      bytes_read:1048576, xml_bytes:9437184, records_read:4321, records_new:4000, rows_written:3000, format:'zip', export_date:null} : null,
    imports:state.imports, sources:[],
  };
}

run(async () => {
  const browser = await launch();
  try {
    for (const viewport of sizes) {
      const context = await browser.newContext({viewport, timezoneId:'Europe/Brussels'});
      const page = await context.newPage();
      const errors = []; page.on('pageerror', error => errors.push(error.message));
      const state = {statusRequests:0, uploads:[], receiver:false, running:false, imports:[], hold:null, answer:null};
      await page.route('**/health', route => route.fulfill({json:fx.health}));
      await page.route('**/api/dashboard?**', route => route.fulfill({json:fx.dashboard(new URL(route.request().url()).searchParams)}));
      await page.route('**/sync/status', route => route.fulfill({json:fx.syncStatus}));
      await page.route('**/api/sync/coverage?**', route => route.fulfill({json:fx.coverage(new URL(route.request().url()).searchParams)}));
      await page.route('**/api/webhooks/status', route => route.fulfill({json:fx.webhookStatus}));
      await page.route('**/api/devices', route => route.fulfill({json:fx.devices}));
      await page.route('**/api/apple/status', route => { state.statusRequests++; return route.fulfill({json:status(state)}); });
      await page.route('**/api/apple/import', async route => {
        const request = route.request();
        state.uploads.push({method:request.method(), headers:await request.allHeaders(), size:(request.postDataBuffer() || Buffer.alloc(0)).length});
        if (state.hold) await state.hold;
        return route.fulfill(state.answer);
      });
      await page.goto(BASE);
      await page.waitForFunction(() => document.querySelectorAll('#history-rows tr').length === 7);
      assert.equal(state.statusRequests, 0, 'the overview never asks for the import status');
      await page.getByRole('button', {name:'Sistem', exact:true}).click();
      // Narrow or short screens show Sistem one group per tab; the panel is in the Cihazlar group.
      if (await page.locator('#system-panels').getAttribute('data-tabbed') !== null) {
        await page.locator('#system-tab-devices').click();
      }
      await until(() => state.statusRequests >= 1, 'status request starts with the System page');
      await page.waitForFunction(() => document.querySelector('#apple-import-receiver').textContent.includes('kapalı'));

      // The panel: heading, the instructions, a labelled file input, Yükle off until a file is chosen.
      const panel = page.locator('#apple-import-panel');
      assert.equal(await page.getByRole('heading', {name:'Apple Sağlık içe aktar'}).count(), 1);
      assert.match(await page.locator('#apple-import-help').textContent(), /Tüm sağlık verilerini dışa aktar/);
      assert.match(await page.locator('#apple-import-help').textContent(), /export\.zip/);
      const input = page.getByLabel('Dışa aktarma dosyası (export.zip veya export.xml)');
      assert.equal(await input.getAttribute('accept'), '.zip,.xml,application/zip,application/xml,text/xml');
      const start = page.getByRole('button', {name:'Yükle', exact:true});
      assert.equal(await start.isDisabled(), true);
      assert.doesNotMatch(await panel.textContent(), /Senkronizasyon|Codex/);
      await inside(page, '#apple-import-panel', viewport);
      await contained(page);

      // Upload held by the mock: the controls lock and the server's progress is polled.
      await input.setInputFiles({name:'dışa aktarılan.zip', mimeType:'application/zip', buffer:ZIP});
      assert.equal(await start.isDisabled(), false);
      let release;
      state.hold = new Promise(resolve => { release = resolve; });
      state.running = true;
      state.answer = {json:DONE};
      const polled = state.statusRequests;
      await start.focus(); await page.keyboard.press('Enter');
      await until(() => state.uploads.length === 1, 'upload reaches the server');
      await until(() => state.statusRequests >= polled + 2, 'server progress is polled while the upload runs');
      await page.waitForFunction(() => document.querySelector('#apple-import-status').textContent.includes('4.321 kayıt okundu'));
      assert.match(await page.locator('#apple-import-status').textContent(), /4\.000 yeni/);
      assert.equal(await page.locator('#apple-import-progress').isVisible(), true);
      assert.equal(await input.isDisabled(), true);
      assert.equal(await start.isDisabled(), true);
      assert.equal(await panel.getAttribute('aria-busy'), 'true');
      await contained(page);
      await page.screenshot({path:shot(`apple-import-running-${viewport.width}.png`)});
      const upload = state.uploads[0];
      assert.equal(upload.method, 'POST');
      assert.equal(upload.headers['x-healthos-request'], '1');
      assert.equal(upload.headers['content-type'], 'application/zip');
      assert.equal(decodeURIComponent(upload.headers['x-apple-export-filename']), 'dışa aktarılan.zip');
      assert.equal(upload.size, ZIP.length, 'the file is the raw request body');

      // Finished: the summary, controls unlocked, polling stops.
      state.running = false; state.imports = [DONE];
      release(); state.hold = null;
      await page.waitForFunction(() => document.querySelector('#apple-import-status').textContent.startsWith('İçe aktarma tamamlandı'));
      const summary = await page.locator('#apple-import-status').textContent();
      assert.match(summary, /123\.456 kayıt okundu · 120\.000 yeni · 4 atlanan öğe · dışa aktarma tarihi 15 Ekim 2026/);
      assert.equal(await page.locator('#apple-import-progress').isHidden(), true);
      assert.equal(await input.isDisabled(), false);
      assert.equal(await start.isDisabled(), true, 'the chosen file is cleared after a successful import');
      assert.equal(await panel.getAttribute('aria-busy'), 'false');
      const settled = state.statusRequests;
      await page.waitForTimeout(1500);
      assert.ok(state.statusRequests <= settled + 1, 'polling stops when the import ends');
      await contained(page);
      await page.screenshot({path:shot(`apple-import-done-${viewport.width}.png`)});

      // A refused file: the server's Turkish reason, shown as a warning, by keyboard.
      state.answer = {status:400, json:{detail:'Apple Sağlık dışa aktarması okunamadı: sentetik bozuk dosya'}};
      await input.setInputFiles({name:'notes.xml', mimeType:'text/xml', buffer:Buffer.from('<notes/>')});
      await start.focus(); await page.keyboard.press('Space');
      await page.waitForFunction(() => document.querySelector('#apple-import-status').textContent.startsWith('İçe aktarılamadı'));
      assert.match(await page.locator('#apple-import-status').textContent(), /sentetik bozuk dosya/);
      assert.equal(await page.locator('#apple-import-status').evaluate(el => el.classList.contains('warning')), true);
      assert.equal(state.uploads[1].headers['content-type'], 'application/xml');
      assert.equal(await start.isDisabled(), false, 'a failed file can be sent again');
      await contained(page);

      // Another import running elsewhere (409) and an answer without detail (413).
      state.answer = {status:409, json:{detail:'Başka bir Apple Sağlık içe aktarması sürüyor.'}};
      await start.click();
      await page.waitForFunction(() => document.querySelector('#apple-import-status').textContent.includes('Başka bir Apple Sağlık'));
      state.answer = {status:413, body:''};
      await start.click();
      await page.waitForFunction(() => document.querySelector('#apple-import-status').textContent.includes('izin verilen boyutu aşıyor'));

      // A very long message scrolls inside the panel, never the page.
      state.answer = {status:400, json:{detail:'Uzun sentetik hata. '.repeat(120)}};
      await start.click();
      await page.waitForFunction(() => document.querySelector('#apple-import-status').textContent.includes('Uzun sentetik hata'));
      assert.equal(await page.locator('#apple-import-status').evaluate(el => el.scrollHeight > el.clientHeight), true);
      await inside(page, '#apple-import-panel', viewport);
      await contained(page);
      await page.screenshot({path:shot(`apple-import-error-${viewport.width}.png`)});

      // Receiver on, read from a refreshed status.
      state.receiver = true;
      await page.getByRole('button', {name:'Durumu yenile', exact:true}).click();
      await page.waitForFunction(() => document.querySelector('#apple-import-receiver').textContent === 'Health Auto Export alıcısı: açık.');
      await contained(page);
      assert.deepEqual(errors, []);
      await context.close();
    }
  } finally { await browser.close(); }
});
