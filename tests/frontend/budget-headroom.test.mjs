import test from 'node:test';
import assert from 'node:assert/strict';
import { baseline, limits } from '../../frontend/build-budget.js';

test('measured budgets add 5% without silently exceeding historical ceilings', () => {
  const ceilings = { js: 60000, css: 20000, deferredJs: 170000, deferredCss: 500, figureAssets: 1950000 };
  for (const [key, { measured, headroomPercent, ceiling }] of Object.entries(baseline.budgets)) {
    assert.ok(Number.isSafeInteger(measured) && measured > 0);
    assert.equal(headroomPercent, 5);
    assert.equal(ceiling, ceilings[key]);
    assert.equal(limits[key], Math.min(ceiling, Math.ceil(measured * 1.05)));
    assert.ok(limits[key] >= measured);
    assert.ok(limits[key] <= ceiling);
  }
});
