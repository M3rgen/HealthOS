import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { readFileSync, readdirSync } from 'node:fs';
import { test } from 'node:test';
import { civilFull, civilRange, civilShort, dateKey, fullDateTime, shiftDay, todayKey, zonedInputValue, zonedInstant } from '../../frontend/src/dates.js';
import { dataTypeLabel, exerciseLabel } from '../../frontend/src/data-types.js';
import { measurementText } from '../../frontend/src/measurement-metrics.js';
import { percent } from '../../frontend/src/format.js';

for (const language of ['tr', 'en']) {
  const cases = [
    ['cross-year range includes the starting year', '2025-12-28', '2026-01-03',
      { tr: '28 Ara 2025 – 3 Ocak 2026', en: '28 Dec 2025 – 3 January 2026' }],
    ['same-year range keeps its format', '2026-03-01', '2026-03-07',
      { tr: '1 Mar – 7 Mart 2026', en: '1 Mar – 7 March 2026' }],
    ['single-day range keeps its format', '2026-03-01', '2026-03-01',
      { tr: '1 Mar – 1 Mart 2026', en: '1 Mar – 1 March 2026' }],
  ];
  for (const [name, start, end, expected] of cases) test(`${language}: ${name}`, () => {
    // A fresh process selects the language before the locale module is loaded.
    const output = execFileSync(process.execPath, ['--input-type=module', '-e', `
      globalThis.localStorage = { getItem: () => ${JSON.stringify(language)} };
      const { civilRange } = await import(${JSON.stringify(new URL('../../frontend/src/dates.js', import.meta.url).href)});
      console.log(civilRange(${JSON.stringify(start)}, ${JSON.stringify(end)}));
    `], { encoding: 'utf8' });
    assert.equal(output.trim(), expected[language]);
  });
}

test('a civil date never shifts with the time zone', () => {
  assert.equal(civilShort('2026-10-16'), '16 Eki');
  assert.equal(civilFull('2026-10-16'), '16 Ekim 2026');
  assert.equal(civilRange('2026-10-09', '2026-10-15'), '9 Eki – 15 Ekim 2026');
});

test('dateKey reads the day in the given zone, around midnight', () => {
  assert.equal(dateKey('2025-12-31T22:30:00Z', 'Europe/Istanbul'), '2026-01-01');
  assert.equal(dateKey('2025-12-31T22:30:00Z', 'UTC'), '2025-12-31');
  assert.match(todayKey('UTC'), /^\d{4}-\d{2}-\d{2}$/);
  assert.equal(shiftDay('2026-03-01', -1), '2026-02-28');
});

test('form instants use the selected zone regardless of the device zone', () => {
  assert.equal(zonedInstant('2026-11-10T08:00', 'Europe/Istanbul').toISOString(), '2026-11-10T05:00:00.000Z');
  for (const zone of ['UTC', 'Europe/Istanbul', 'Europe/Brussels', 'America/New_York', 'Asia/Kathmandu']) {
    for (const value of ['2026-01-01T00:00', '2026-07-15T08:45', '2026-11-10T08:00']) {
      assert.equal(zonedInputValue(zonedInstant(value, zone), zone), value, `${zone}: ${value}`);
    }
  }
  assert.equal(zonedInputValue('2026-11-10T21:00:59Z', 'Europe/Istanbul'), '2026-11-11T00:00');
  assert.equal(zonedInputValue('0099-01-01T00:00:00Z', 'UTC'), '0099-01-01T00:00');
});

for (const [zone, wall, instant, resolved] of [
  ['Europe/Brussels', '2026-03-29T02:30', '2026-03-29T01:30:00.000Z', '2026-03-29T03:30'],
  ['Europe/Brussels', '2026-10-25T02:30', '2026-10-25T00:30:00.000Z', '2026-10-25T02:30'],
  ['Australia/Lord_Howe', '2026-10-04T02:15', '2026-10-03T15:45:00.000Z', '2026-10-04T02:45'],
  ['Australia/Lord_Howe', '2026-04-05T01:45', '2026-04-04T14:45:00.000Z', '2026-04-05T01:45'],
  ['Pacific/Apia', '2011-12-30T12:00', '2011-12-30T22:00:00.000Z', '2011-12-31T12:00'],
]) {
  test(`compatible offset transition: ${zone} ${wall}`, () => {
    const date = zonedInstant(wall, zone);
    assert.equal(date.toISOString(), instant);
    assert.equal(zonedInputValue(date, zone), resolved);
  });
}

test('a full moment reads as words and minutes, not "16.10.2026 15:58:00"', () => {
  const text = fullDateTime('2026-10-16T12:58:00Z', 'Europe/Istanbul');
  assert.equal(text, '16 Ekim 2026 15:58');
  assert.doesNotMatch(text, /:\d\d:\d\d/);
});

test('percent has one form everywhere and body measurements use it', () => {
  assert.equal(percent(97.8), '%97,8');
  assert.equal(percent(null), '—');
  assert.equal(measurementText('body_fat', 17.8), '%17,8');
  assert.equal(measurementText('muscle', 41.25, 2), '%41,25');
  assert.equal(measurementText('weight', 74.6), '74,6 kg');
  assert.equal(measurementText('height', 178), '178 cm');
});

test('codes become words, and a known one becomes its name', () => {
  assert.equal(dataTypeLabel('daily-resting-heart-rate'), 'Günlük dinlenme nabzı');
  assert.equal(dataTypeLabel('brand-new-type'), 'Brand new type');
  assert.equal(exerciseLabel('RUN_WALK'), 'Koşu-yürüyüş');
  assert.equal(exerciseLabel('Yürüyüş'), 'Yürüyüş', 'a typed name passes through');
  assert.equal(exerciseLabel('TABLE_TENNIS'), 'Table tennis');
  assert.equal(exerciseLabel(null), null);
});

test('date keys and civil parsing live only in dates.js', () => {
  const src = new URL('../../frontend/src/', import.meta.url);
  for (const file of readdirSync(src).filter(name => name.endsWith('.js') && name !== 'dates.js')) {
    const source = readFileSync(new URL(file, src), 'utf8');
    assert.ok(!source.includes("'en-CA'"), `${file} builds a date key itself`);
    assert.ok(!source.includes('T12:00:00Z'), `${file} parses a civil date itself`);
  }
});

test('no module keeps a UI state flag in a DOM property for motion', () => {
  const src = new URL('../../frontend/src/', import.meta.url);
  for (const file of readdirSync(src).filter(name => name.endsWith('.js'))) {
    const source = readFileSync(new URL(file, src), 'utf8');
    assert.ok(!/\$\('model-motion'\)\.checked\s*[!=]==|getMotion: \(\) => \$\('model-motion'\)/.test(source), `${file} reads motion from the DOM`);
  }
});
