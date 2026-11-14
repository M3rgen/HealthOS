import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const fx = require('../../frontend/preview/fixtures.cjs');

const dashboard = fx.dashboard(new URLSearchParams()).days;
const trNumber = value => String(value).replace(/\B(?=(\d{3})+(?!\d))/g, '.');

test('every metric history page shows the dashboard value for the same day', () => {
  for (const metric of ['steps', 'sleep', 'resting_bpm']) {
    const key = { steps: 'steps', sleep: 'sleep_seconds', resting_bpm: 'resting_bpm' }[metric];
    const history = fx.history(new URLSearchParams({ metric })).days;
    for (const row of dashboard) assert.equal(history.find(day => day.date === row.date).value, row[key], `${metric} ${row.date}`);
  }
  const today = fx.history(new URLSearchParams({ metric: 'steps' })).days.at(-1);
  assert.deepEqual([today.date, today.partial], [fx.TODAY, true]);
  // A past window of the dashboard agrees with the history page that holds those days.
  const past = fx.dashboard(new URLSearchParams({ end_date: '2026-09-20' })).days;
  const second = fx.history(new URLSearchParams({ metric: 'steps', page: '2' })).days;
  for (const row of past) assert.equal(second.find(day => day.date === row.date)?.value ?? row.steps, row.steps, row.date);
});

test('the history keeps empty days, none of them inside the dashboard week', () => {
  const empty = fx.history(new URLSearchParams()).days.filter(day => day.value === null).map(day => day.date);
  assert.ok(empty.length >= 2);
  assert.ok(empty.every(date => !dashboard.some(row => row.date === date)));
});

test("Umay's preview answer is written from the dashboard's last 7 days", () => {
  const done = dashboard.filter(row => !row.partial);
  const mean = key => Math.round(done.reduce((sum, row) => sum + row[key], 0) / done.length);
  const steps = done.map(row => row.steps);
  for (const value of [mean('steps'), Math.max(...steps), Math.min(...steps), dashboard.find(row => row.partial).steps])
    assert.ok(fx.chatAnswer.includes(trNumber(value)), `${trNumber(value)} in the answer`);
  const numbers = fx.chatAnswer.match(/\d{1,3}(\.\d{3})+/g);
  assert.deepEqual(numbers.filter(n => ![mean('steps'), ...steps, dashboard[0].steps].map(trNumber).includes(n)), []);
  const receipt = fx.chatTransfer('completed');
  assert.equal(receipt.counts.daily_summaries, dashboard.length);
  assert.deepEqual(receipt.calls[0].data_dates, { first: dashboard.at(-1).date, last: dashboard[0].date });
});
