import assert from 'node:assert/strict';
import { test } from 'node:test';
import { setupMeasurementList } from '../../frontend/src/measurement-list.js';
import { escape } from '../../frontend/src/format.js';
import { t as strings } from '../../frontend/src/i18n/index.js';

test('the limit notice is last at 100 or more rows and disappears below the cap', async t => {
  const body = { innerHTML: '', contains: () => false };
  const previous = globalThis.document;
  t.after(() => { globalThis.document = previous; });
  globalThis.document = { getElementById: () => body };
  let count;
  const controller = setupMeasurementList({
    request: async () => Array.from({ length: count }, (_, id) => ({
      id, metric: 'weight', value: 70, measured_at: '2026-01-01T00:00:00Z',
    })),
    notice: () => assert.fail('loading must succeed'), clearNotice: () => {},
  });
  for (count of [100, 101, 99, 0]) {
    await controller.load();
    const rows = body.innerHTML.match(/<tr[\s\S]*?<\/tr>/g);
    assert.equal(rows.length, count ? count + Number(count >= 100) : 1);
    assert.equal(body.innerHTML.includes('En yeni 100 ölçüm'), count >= 100);
    if (count >= 100) assert.match(rows.at(-1), /^<tr><td colspan="5">En yeni 100 ölçüm.*<\/td><\/tr>$/);
  }
});

test('measurement rows and empty/failure messages escape markup characters', async t => {
  const body = { innerHTML: '', contains: () => false };
  const previous = globalThis.document;
  const { empty, loadFailed } = strings.measurements;
  t.after(() => {
    globalThis.document = previous;
    Object.assign(strings.measurements, { empty, loadFailed });
  });
  globalThis.document = { getElementById: () => body };
  const payload = '<img src=x onerror="unsafe()"> & " \'', encoded = escape(payload);
  Object.assign(strings.measurements, { empty: payload, loadFailed: payload });
  let mode = 'rows', notices = 0;
  const controller = setupMeasurementList({
    request: async () => {
      if (mode === 'failed') throw new Error('synthetic');
      return mode === 'empty' ? [] : [{ id: payload, metric: payload, value: 70, measured_at: '2026-01-01T00:00:00Z' }];
    },
    notice: () => notices++, clearNotice: () => {},
  });
  for (mode of ['rows', 'empty', 'failed']) {
    await controller.load();
    assert.ok(body.innerHTML.includes(encoded));
    assert.ok(!body.innerHTML.includes(payload));
    if (mode === 'rows') assert.ok(body.innerHTML.includes(`data-flag-id="${encoded}"`));
    else assert.equal(body.innerHTML, `<tr><td colspan="5">${encoded}</td></tr>`);
  }
  assert.equal(notices, 1);
});
