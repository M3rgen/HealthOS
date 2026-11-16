import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { dayTypes, mealTotals, priceTotals, programWeek, sections, weekday } from '../../frontend/src/plan-model.js';
import { setPlanLanguage, text } from '../../frontend/src/i18n/plan.js';
import { useFormat } from '../../frontend/src/plan-format.js';
import { civilDate, civilParts, shiftDay } from '../../frontend/src/dates.js';
import { number } from '../../frontend/src/locale.js';

useFormat({ civilDate, civilParts, shiftDay, number });

const require = createRequire(import.meta.url);
const sample = require('../../docs/examples/plan.sample.json');

test('day selection uses ISO weekdays, preserves overlapping types and does not invent a rest day', () => {
  assert.equal(weekday('2026-03-29'), 7);
  const days = [...sample.day_types, { id: 'synthetic-overlap', weekdays: [1] }];
  assert.deepEqual(dayTypes(days, '2026-03-30').map(row => row.id), ['example-work', 'synthetic-overlap']);
  assert.deepEqual(dayTypes(days, '2026-03-29'), []);
  assert.deepEqual(dayTypes([{ id: 'synthetic-unassigned' }], '2026-03-30'), []);
});

test('program weeks use civil days across DST, month and year boundaries', () => {
  assert.equal(programWeek(null, '2026-03-30'), null);
  assert.equal(programWeek('2026-03-30', '2026-03-29'), 0);
  assert.equal(programWeek('2026-03-23', '2026-03-23'), 1);
  assert.equal(programWeek('2026-03-23', '2026-03-29'), 1);
  assert.equal(programWeek('2026-03-23', '2026-03-30'), 2);
  assert.equal(programWeek('2025-12-29', '2026-01-05'), 2);
  assert.equal(programWeek('invalid', '2026-03-30'), null);
});

test('a start date that is not a Monday counts weeks from the Monday of its week', () => {
  // 2026-03-25 is a Wednesday; its week runs Monday 2026-03-23 to Sunday 2026-03-29.
  assert.equal(programWeek('2026-03-25', '2026-03-22'), 0, 'the Sunday before that week');
  assert.equal(programWeek('2026-03-25', '2026-03-23'), 1, 'the Monday before the start day');
  assert.equal(programWeek('2026-03-25', '2026-03-29'), 1);
  assert.equal(programWeek('2026-03-25', '2026-03-30'), 2, 'the next Monday');
  assert.equal(programWeek('2026-03-29', '2026-03-30'), 2, 'a Sunday start has its second week a day later');
  assert.equal(programWeek('2026-10-30', '2026-10-26'), 1, 'across the end of summer time');
  assert.equal(programWeek('2026-10-30', '2026-11-02'), 2);
});

test('meal totals include explicit values including zero, exclude missing values', () => {
  assert.deepEqual(mealTotals(sample.day_types[0].meals), { kcal: 500, protein: 20 });
  assert.deepEqual(mealTotals([{ kcal: 0, protein_g: null }, {}, { kcal: 100.5, protein_g: 4.5 }]), { kcal: 100.5, protein: 4.5 });
  assert.deepEqual(mealTotals([]), { kcal: 0, protein: 0 });
});

test('price totals exclude unpriced items and keep document currencies separate', () => {
  const items = [...sample.shopping.groups[0].items, ...sample.shopping.one_off];
  assert.deepEqual(priceTotals(items), { EUR: 30 });
  assert.deepEqual(priceTotals([...items, { estimated_price: 5, currency: 'USD' },
    { estimated_price: null, currency: null }, { estimated_price: 0, currency: 'GBP' }, {}]), { EUR: 30, USD: 5, GBP: 0 });
  assert.deepEqual(priceTotals([]), {});
});

test('navigation hides absent and empty sections, including empty shopping groups', () => {
  assert.deepEqual(sections(sample), ['today', 'nutrition', 'batch', 'shopping', 'training', 'measurements', 'assumptions']);
  assert.deepEqual(sections({ schema: 1 }), []);
  assert.deepEqual(sections({ schema: 1, targets: { cards: [], adjustment_rules: [] }, training: { rules: [] },
    day_types: [{ id: 'synthetic-empty', name: 'Synthetic', weekdays: [1], meals: [] }],
    shopping: { groups: [{ name: 'Synthetic', items: [] }], one_off: [] }, measurements: [], assumptions: [] }), []);
  assert.deepEqual(sections({ schema: 1, meta: { warning: 'Synthetic warning' } }), ['assumptions']);
  assert.deepEqual(sections({ schema: 1, measurements: ['Synthetic instruction'] }), ['measurements']);
});

test('preview plan uses the public fictional example without duplicating its content', () => {
  assert.deepEqual(require('../../frontend/preview/fixtures.cjs').plan.plan, sample);
  const source = readFileSync(new URL('../../frontend/src/plan.js', import.meta.url), 'utf8');
  assert.ok(!source.includes('innerHTML'));
});

test('Plan translations have matching nonempty entries in both languages', () => {
  setPlanLanguage('tr');
  const turkish = { ...text };
  setPlanLanguage('en');
  assert.deepEqual(Object.keys(text), Object.keys(turkish));
  for (const value of Object.values(text)) assert.ok(typeof value === 'string' && value.trim());
  setPlanLanguage('tr');
});
