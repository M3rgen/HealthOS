import test from 'node:test';
import assert from 'node:assert/strict';

// Each test loads api.js afresh: the "already redirecting" latch is module state.
let serial = 0;
const fresh = () => import(`../../frontend/src/api.js?case=${++serial}`);

function environment(t, answer, href = { pathname: '/', search: '', hash: '' }) {
  const assigned = [], calls = [];
  const saved = ['fetch', 'location'].map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]);
  t.after(() => saved.forEach(([key, descriptor]) => {
    if (descriptor) Object.defineProperty(globalThis, key, descriptor);
    else delete globalThis[key];
  }));
  Object.defineProperty(globalThis, 'location', { configurable: true, value: { ...href, assign: url => assigned.push(url) } });
  Object.defineProperty(globalThis, 'fetch', { configurable: true, value: async (url, options) => {
    calls.push({ url, options });
    if (answer instanceof Error) throw answer;
    return answer;
  } });
  return { assigned, calls };
}
const json = (status, body) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
const failure = async promise => { try { await promise; } catch (error) { return error; } throw new Error('expected a rejection'); };

test('a 401 from request() goes to the login page and returns to the open page afterwards', async t => {
  const env = environment(t, json(401, { detail: 'auth_required' }), { pathname: '/', search: '?a=1', hash: '#/sports' });
  const { request, ApiError } = await fresh();
  const error = await failure(request('/api/dashboard'));
  assert.ok(error instanceof ApiError);
  assert.equal(error.kind, 'unauthorized');
  assert.deepEqual(env.assigned, [`/login?${new URLSearchParams({ next: '/?a=1#/sports' })}`]);
});

test('a 401 from /health redirects too, and concurrent 401s redirect once', async t => {
  const env = environment(t, json(401, {}));
  const { fetchHealth, request } = await fresh();
  await Promise.allSettled([fetchHealth(), request('/x')]);
  assert.equal(env.assigned.length, 1);
  assert.match(env.assigned[0], /^\/login\?next=/);
});

test('other refused statuses never redirect', async t => {
  for (const status of [400, 403, 404, 422, 500, 503]) {
    const env = environment(t, json(status, { detail: 'no' }));
    const { request } = await fresh();
    await failure(request('/x'));
    assert.deepEqual(env.assigned, [], `status ${status}`);
  }
});

test('request() names the failure: refused, server, malformed, network, timeout', async t => {
  const cases = [
    // A validator's list (English, field by field) is not shown; the status names the failure.
    [json(422, { detail: [{ msg: 'bir' }, { msg: 'iki' }] }), 'rejected', 'Girilen değerler geçersiz; alanları kontrol et.'],
    [json(500, { detail: 'kırık' }), 'server', 'kırık'],
    [new Response('<html>502</html>', { status: 502 }), 'malformed'],
    [new Response('<html>ok</html>', { status: 200 }), 'malformed'],
    [new TypeError('Failed to fetch'), 'network'],
    [Object.assign(new Error('late'), { name: 'TimeoutError' }), 'timeout'],
  ];
  for (const [answer, kind, message] of cases) {
    environment(t, answer);
    const { request, ApiError } = await fresh();
    const error = await failure(request('/x'));
    assert.ok(error instanceof ApiError, kind);
    assert.equal(error.kind, kind);
    if (message) assert.equal(error.message, message);
  }
});

test('request() returns the body of a success and sends the caller signal with its timeout', async t => {
  const env = environment(t, json(200, [1, 2]));
  const { request } = await fresh();
  const controller = new AbortController();
  assert.deepEqual(await request('/x', { signal: controller.signal }), [1, 2]);
  const { signal, cache } = env.calls[0].options;
  assert.equal(cache, 'no-store');
  assert.notEqual(signal, controller.signal, 'combined with the timeout');
  controller.abort();
  assert.equal(signal.aborted, true);
});

test('fetchHealth: a 500 with a JSON body is a failure, not a report of an empty service', async t => {
  environment(t, json(500, { detail: 'Internal Server Error' }));
  const { fetchHealth } = await fresh();
  const error = await failure(fetchHealth());
  assert.equal(error.kind, 'server');
  assert.equal(error.status, 500);
});

test('fetchHealth: the documented 503 report is still read; other 503 bodies are not', async t => {
  const report = { status: 'failed', database: 'unavailable', service: 'healthos' };
  environment(t, json(503, report));
  assert.deepEqual(await (await fresh()).fetchHealth(), report);
  environment(t, json(503, { detail: 'proxy' }));
  assert.equal((await failure((await fresh()).fetchHealth())).kind, 'server');
  environment(t, json(503, { status: 'healthy' }));
  assert.equal((await failure((await fresh()).fetchHealth())).kind, 'server');
});

test('fetchHealth: a 200 that is no report and an unreadable body are failures', async t => {
  environment(t, json(200, { detail: 'hm' }));
  assert.equal((await failure((await fresh()).fetchHealth())).kind, 'malformed');
  environment(t, new Response('nope', { status: 200 }));
  assert.equal((await failure((await fresh()).fetchHealth())).kind, 'malformed');
  environment(t, new TypeError('down'));
  assert.equal((await failure((await fresh()).fetchHealth())).kind, 'network');
});
