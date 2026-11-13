import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';
import { createDashboardData, isDashboardPayload } from '../../frontend/src/dashboard-data.js';
import { createPolling } from '../../frontend/src/polling.js';
import { state } from '../../frontend/src/state.js';

const good = { days: [{ date: '2026-10-16' }], charts: { steps: [], heart_rate: [] }, profile: {} };
afterEach(() => { delete globalThis.document; });

function setup(overrides = {}) {
  Object.assign(state, { data: null, dataReceivedAt: 0, health: null, busy: false, pendingRefresh: false, activePage: 'overview', zone: 'UTC' });
  const calls = { notices: [], cleared: [], painted: [], reported: [], known: 0, after: 0, retried: 0 };
  const caption = { textContent: '10 Eki – 16 Ekim 2026' };
  const cards = { stale: false, toggleAttribute: (_name, value) => { cards.stale = value; } };
  globalThis.document = { getElementById: () => caption, querySelector: () => cards };
  const dashboard = createDashboardData({
    request: url => (url.startsWith('/api/dashboard') ? Promise.resolve(good) : Promise.resolve([])),
    fetchHealth: () => Promise.resolve({ status: 'healthy', failed_syncs: 0 }),
    query: () => 'days=7',
    paint: {
      data: () => calls.painted.push('data'),
      dataFailed: () => calls.painted.push('dataFailed'),
      health: () => calls.painted.push('health'),
      healthFailed: () => calls.painted.push('healthFailed'),
    },
    notice: (...args) => calls.notices.push(args),
    clearNotice: (...sources) => calls.cleared.push(...sources),
    onHealthFirstKnown: () => calls.known++,
    afterRefresh: () => calls.after++,
    retry: () => calls.retried++,
    report: error => calls.reported.push(error),
    ...overrides,
  });
  return { dashboard, calls, cards, caption };
}

test('the payload guard accepts only what paint code relies on', () => {
  assert.ok(isDashboardPayload(good));
  for (const value of [null, {}, { days: [], charts: {}, profile: {} }, { days: [{}], profile: {} }, { days: 'x', charts: {}, profile: {} }])
    assert.equal(isDashboardPayload(value), false, JSON.stringify(value));
});

test('a response with days: [] counts as a failed load and never throws', async () => {
  const { dashboard, calls } = setup({ request: () => Promise.resolve({ days: [], charts: {}, profile: {} }) });
  await dashboard.refresh();
  assert.deepEqual(calls.painted, ['dataFailed', 'health']);
  assert.equal(calls.notices[0][2], 'dashboard');
  assert.equal(calls.notices[0][4].label, 'Yeniden dene');
  calls.notices[0][4].run();
  assert.equal(calls.retried, 1, 'the notice carries a retry control');
  assert.equal(state.busy, false);
  assert.equal(state.data, null);
});

test('a throwing painter reports, shows the retry and releases the busy flag', async () => {
  const boom = new Error('paint');
  const { dashboard, calls } = setup({
    paint: { data() { throw boom; }, dataFailed() {}, health() {}, healthFailed() {} },
  });
  await dashboard.refresh();
  assert.deepEqual(calls.reported, [boom]);
  assert.equal(calls.notices.length, 1);
  assert.equal(state.busy, false);
  assert.equal(calls.after, 1, 'the calendar and the other follow-ups still run');
});

test('an unreachable service marks health failed; a later success keeps the last painted data', async () => {
  const { dashboard, calls } = setup({ fetchHealth: () => Promise.reject(new Error('offline')) });
  await dashboard.refresh();
  assert.ok(calls.painted.includes('healthFailed'));
  assert.equal(state.health, null);
  assert.equal(state.data, good);
  // The next load fails: painted data stays, the empty-state text is not written over it.
  const second = setup({ request: () => Promise.reject(new Error('offline')) });
  Object.assign(state, { data: good });
  await second.dashboard.refresh();
  assert.ok(!second.calls.painted.includes('dataFailed'));
  assert.equal(second.calls.notices.length, 1);
});

test('a refresh asked for while one runs happens once more afterwards', async () => {
  let open = 0;
  const { dashboard } = setup({ request: () => { open++; return Promise.resolve(good); } });
  const first = dashboard.refresh();
  await dashboard.refresh();
  await first;
  await new Promise(resolve => setTimeout(resolve));
  assert.equal(open, 2);
  assert.equal(state.busy, false);
});

for (const failedSyncs of [0, 1]) {
  test(`dashboard failure neither shows nor clears sync notices (${failedSyncs} failed syncs), recovery resumes them`, async () => {
    let failed = true;
    const { dashboard, calls } = setup({
      request: url => url.startsWith('/api/dashboard')
        ? (failed ? Promise.reject(new Error('offline')) : Promise.resolve(good))
        : Promise.resolve([{ data_type: 'steps', status: 'failed', last_error: 'rate_limited' }]),
      fetchHealth: () => Promise.resolve({ status: 'degraded', failed_syncs: failedSyncs }),
    });
    await dashboard.refresh();
    assert.equal(calls.notices.at(-1)[2], 'dashboard');
    assert.deepEqual(calls.notices.map(call => call[2]), ['dashboard']);
    assert.deepEqual(calls.cleared, [], 'even clearing sync is deferred on dashboard failure');
    failed = false;
    await dashboard.refresh();
    assert.ok(calls.cleared.includes('dashboard'));
    if (failedSyncs) assert.equal(calls.notices.at(-1)[2], 'sync');
    else assert.ok(calls.cleared.includes('sync'));
  });
}

test('old data keeps its receipt time and one stale caption until a successful paint', async t => {
  t.mock.timers.enable({ apis: ['Date'], now: Date.parse('2026-10-16T15:58:00Z') });
  let failed = false;
  const { dashboard, cards, caption } = setup({ request: () => failed ? Promise.reject(new Error('offline')) : Promise.resolve(good) });
  await dashboard.refresh();
  const receivedAt = state.dataReceivedAt;
  assert.equal(receivedAt, Date.now());
  assert.equal(cards.stale, false);
  failed = true;
  t.mock.timers.tick(120000);
  for (let i = 0; i < 2; i++) await dashboard.refresh();
  assert.equal(state.data, good);
  assert.equal(state.dataReceivedAt, receivedAt);
  assert.equal(cards.stale, true);
  assert.equal(caption.textContent, '10 Eki – 16 Ekim 2026 · güncellenemedi, son veri 15:58');
  failed = false;
  await dashboard.refresh();
  assert.equal(cards.stale, false);
  assert.equal(state.dataReceivedAt, Date.now());
});

test('polling survives a failing tick and can be paused and stopped', async () => {
  let timer = null, runs = 0, paused = false;
  const reported = [];
  const polling = createPolling(async () => { runs++; throw new Error('tick'); }, {
    paused: () => paused,
    timers: { setInterval: fn => { timer = fn; return 1; }, clearInterval: () => { timer = null; } },
    report: error => reported.push(error.message),
  });
  polling.start();
  assert.equal(polling.running, true);
  await timer();
  await timer();
  assert.equal(runs, 2);
  assert.deepEqual(reported, ['tick', 'tick']);
  paused = true;
  await timer();
  assert.equal(runs, 2, 'a hidden page does not poll');
  polling.stop();
  assert.equal(polling.running, false);
  assert.equal(timer, null);
});
