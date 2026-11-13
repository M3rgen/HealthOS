import assert from 'node:assert/strict';
import { test } from 'node:test';
import { paintCards } from '../../frontend/src/overview-cards.js';
import { bars } from '../../frontend/src/sparklines.js';

const titles = markup => [...markup.matchAll(/<title>(.*?)<\/title>/g)].map(match => match[1]);

test('sleep dates and durations follow the bars from oldest to newest; steps share their hourly labels', t => {
  const elements = new Map();
  const previous = globalThis.document;
  t.after(() => { if (previous === undefined) delete globalThis.document; else globalThis.document = previous; });
  globalThis.document = { getElementById: id => {
    if (!elements.has(id)) elements.set(id, {
      classList: { toggle() {} },
      setAttribute(key, value) { this[key] = value; },
    });
    return elements.get(id);
  } };
  const days = [
    { date: '2026-10-16', sleep_seconds: 26760 },
    { date: '2026-10-13', sleep_seconds: 25200 },
    { date: '2026-10-10', sleep_seconds: 25080 },
  ];
  paintCards({ days, profile: {}, charts: { steps: [0, null, 1234], heart_rate: [60] } }, {
    profileValues: { update() {} }, body: { setBodyMetrics() {} },
  });
  const sleep = elements.get('sleep-chart'), steps = elements.get('steps-chart');
  assert.deepEqual(titles(sleep.innerHTML), ['10 Eki: 6 sa 58 dk', '13 Eki: 7 sa 0 dk', '16 Eki: 7 sa 26 dk']);
  assert.equal(sleep['aria-label'], 'Günlük uyku süreleri: 10 Eki: 6 sa 58 dk, 13 Eki: 7 sa 0 dk, 16 Eki: 7 sa 26 dk');
  assert.deepEqual(titles(steps.innerHTML), ['0:00 0', '2:00 1.234']);
  assert.equal(steps['aria-label'], 'Seçilen günün saatlik adımları: 0:00 0, 1:00 —, 2:00 1.234');
  assert.equal(days[0].date, '2026-10-16', 'rendering must keep the dashboard day order');
});

test('bar labels are escaped for both zero and positive values and keep their indices across gaps', () => {
  const markup = bars([0, null, 10], 'red', 'blue', ['<zero>&', 'missing', '\"ten\"']);
  assert.deepEqual(titles(markup), ['&lt;zero&gt;&amp;', '&quot;ten&quot;']);
});

test('bars without labels keep the existing index and formatted-value titles', () => {
  const values = [0, null, 1234.5];
  const markup = bars(values, 'red', 'blue');
  assert.deepEqual(titles(markup), ['0: 0', '2: 1.234,5']);
  assert.equal(markup, bars(values, 'red', 'blue', ['0: 0', 'unused', '2: 1.234,5']));
});
