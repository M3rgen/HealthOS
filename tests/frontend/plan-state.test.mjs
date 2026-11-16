import test from 'node:test';
import assert from 'node:assert/strict';
import { createPlanState } from '../../frontend/src/plan-state.js';

const initial = () => ({ start_date: null, checked_item_ids: [] });
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
const tick = async t => { await t.mock.timers.tick(120); await Promise.resolve(); await Promise.resolve(); };

function setup(t) {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const requests = [], changes = [];
  let failures = 0;
  const store = createPlanState({ initial: initial(),
    write: state => { const response = deferred(); requests.push({ state, ...response }); return response.promise; },
    changed: (state, saving, succeeded) => changes.push({ state, saving, succeeded }), failed: () => failures++,
  });
  return { store, requests, changes, failures: () => failures };
}

test('rapid checks and the start date are optimistic and coalesced into one full state PUT', async t => {
  const { store, requests, changes } = setup(t);
  store.change({ checked_item_ids: ['example-grain'] });
  assert.deepEqual(changes.at(-1).state.checked_item_ids, ['example-grain']);
  store.change({ start_date: '2026-01-01' });
  store.change({ checked_item_ids: ['example-grain', 'example-box'] });
  assert.equal(requests.length, 0);
  await tick(t);
  assert.equal(requests.length, 1);
  assert.deepEqual(requests[0].state, { start_date: '2026-01-01', checked_item_ids: ['example-grain', 'example-box'] });
  requests[0].resolve(requests[0].state);
  await Promise.resolve();
  assert.equal(store.busy, false);
  assert.equal(changes.at(-1).succeeded, true);
});

test('failed writes roll back and a newer independent edit is replayed, without resurrecting the failed check', async t => {
  const { store, requests, changes, failures } = setup(t);
  store.change({ checked_item_ids: ['example-grain'] });
  await tick(t);
  store.change({ checked_item_ids: ['example-grain', 'example-box'] });
  store.replace({ start_date: '2025-01-01', checked_item_ids: [] });
  requests[0].reject(new Error('synthetic'));
  await Promise.resolve();
  assert.equal(failures(), 1);
  assert.deepEqual(requests[1].state, { start_date: null, checked_item_ids: ['example-box'] });
  requests[1].resolve(requests[1].state);
  await Promise.resolve();
  assert.deepEqual(changes.at(-1).state.checked_item_ids, ['example-box']);
  assert.equal(store.busy, false);
});

test('reset and date failures return to confirmed server state and are not announced as saved', async t => {
  const { store, requests, changes } = setup(t);
  const confirmed = { start_date: '2026-01-01', checked_item_ids: ['example-grain'] };
  store.replace(confirmed);
  store.change({ checked_item_ids: [], start_date: null });
  await tick(t);
  requests[0].reject(new Error('synthetic'));
  await Promise.resolve();
  assert.deepEqual(changes.at(-1), { state: confirmed, saving: false, succeeded: false });
});

test('a later edit waits for the earlier PUT and survives its successful response', async t => {
  const { store, requests } = setup(t);
  store.change({ checked_item_ids: ['example-grain'] });
  await tick(t);
  store.change({ checked_item_ids: [] });
  await tick(t);
  assert.equal(requests.length, 1);
  requests[0].resolve(requests[0].state);
  await Promise.resolve();
  assert.deepEqual(requests[1].state.checked_item_ids, []);
  requests[1].resolve(requests[1].state);
  await Promise.resolve();
  assert.equal(store.busy, false);
});
