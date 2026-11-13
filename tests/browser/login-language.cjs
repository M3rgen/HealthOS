// Render the production login HTML/CSP; no app server, credentials or archive needed.
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const path = require('node:path');
const { BASE, launch, run, shot } = require('./lib.cjs');

const root = path.resolve(__dirname, '../..');
// The frontend CI job has no backend virtualenv. Load just the production renderer and its
// constants with the standard library; only the framework response envelope is substituted.
const variants = JSON.parse(execFileSync('python3', ['-I', '-S', '-c', `
import ast, base64, hashlib, json
from html import escape
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode, urlsplit

def response(content, status_code=200, headers=None):
    headers = {key.lower(): value for key, value in (headers or {}).items()}
    return SimpleNamespace(body=content.encode(), headers={
        **headers, 'content-type': 'text/html; charset=utf-8'})

scope = dict(globals(), HTMLResponse=response)
names = {'COOKIE_MAX_AGE', 'LOGIN_PATH', 'safe_next',
         'STYLE', 'STYLE_HASH', 'CSP', 'ERRORS', 'TEXT', 'page'}
for filename in ('app/core/auth.py', 'app/api/login.py'):
    nodes = []
    for node in ast.parse(Path(filename).read_text()).body:
        if ((isinstance(node, ast.ImportFrom) and node.module == '__future__') or
            (isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id in names for target in node.targets)) or
            (isinstance(node, ast.FunctionDef) and node.name in names)):
            nodes.append(node)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), filename, 'exec'), scope)
page = scope['page']
responses = {}
for lang in ('tr', 'en'):
    for error in (None, 'token', 'origin', 'rate'):
        response = page('/api/probe?a=1&b=2', error=error, lang=lang,
                        remember_seconds=86400, isolated='http://healthos.localhost/login')
        responses[f'{lang}:{error or "none"}'] = {
            'body': response.body.decode(), 'headers': dict(response.headers)}
print(json.dumps(responses))
`], { cwd: root, encoding: 'utf8' }));

async function contained(page) {
  assert(await page.evaluate(() => {
    const root = document.scrollingElement;
    const panel = document.querySelector('main').getBoundingClientRect();
    return root.scrollHeight <= innerHeight && root.scrollWidth <= innerWidth &&
      panel.top >= 0 && panel.bottom <= innerHeight && panel.left >= 0 && panel.right <= innerWidth;
  }), 'login panel fits; the main page never scrolls');
  // Even at the shortest floor all content remains reachable inside the panel.
  for (const selector of ['#token', 'button', 'a[hreflang]']) {
    await page.locator(selector).focus();
    await page.locator(selector).scrollIntoViewIfNeeded();
    assert(await page.locator(selector).evaluate(element => {
      const box = element.getBoundingClientRect();
      return box.top >= 0 && box.bottom <= innerHeight && box.left >= 0 && box.right <= innerWidth;
    }), `${selector} is reachable`);
  }
  assert(await page.evaluate(() => scrollX === 0 && scrollY === 0), 'focus does not scroll the page');
}

run(async () => {
  const browser = await launch();
  try {
    for (const [width, height] of [[1280, 800], [390, 844], [320, 640], [844, 390], [320, 256], [1280, 256]]) {
      const page = await browser.newPage({ viewport: { width, height } });
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      let error = 'none';
      await page.route('**/login?**', route => {
        const lang = new URL(route.request().url()).searchParams.get('lang') || 'tr';
        return route.fulfill({ status: 200, ...variants[`${lang}:${error}`] });
      });
      for (const lang of ['tr', 'en']) {
        for (error of ['none', 'token', 'origin', 'rate']) {
          await page.goto(`${BASE}/login?lang=${lang}`);
          await page.locator('#token').waitFor({ state: 'visible' });
          assert.equal(await page.locator('html').getAttribute('lang'), lang);
          assert.equal(await page.locator('input[name="lang"]').inputValue(), lang);
          assert.equal(await page.locator('[role="alert"]').count(), error === 'none' ? 0 : 1);
          await contained(page);
        }
        error = 'none';
        const link = page.locator('a[hreflang]');
        const other = lang === 'tr' ? 'en' : 'tr';
        assert.equal(await link.textContent(), lang === 'tr' ? 'English' : 'Türkçe');
        assert.equal(await link.getAttribute('lang'), other);
        await Promise.all([page.waitForURL(url => url.searchParams.get('lang') === other), link.click()]);
        assert.equal(new URL(page.url()).searchParams.get('next'), '/api/probe?a=1&b=2');
        assert.equal(await page.locator('html').getAttribute('lang'), other);
        await contained(page);
        assert.equal(await page.evaluate(() => localStorage.getItem('healthos.language')), null);
        if (height === 844) await page.screenshot({ path: shot(`login-language-${other}.png`) });
      }
      assert.deepEqual(errors, []);
      assert.deepEqual(await page.context().cookies(), []);
      await page.close();
      console.log(`PASS ${width}x${height}: both login languages, all errors, safe language switch, no page scrolling`);
    }
  } finally { await browser.close(); }
});
