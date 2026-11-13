import assert from 'node:assert/strict';
import { test } from 'node:test';
import { historySheetBounds } from '../../frontend/src/history-sheet.js';

test('history drag follows distance and recomputes all bounds for mobile and late warning', () => {
  const desktop = {top: 504, bottom: 776, left: 12, right: 1268};
  const openness = 70 / (desktop.top - 90);
  const dragged = historySheetBounds(desktop, {width: 1280, height: 800}, 90, openness);
  assert.equal(dragged.top, desktop.top - 70);
  const mobile = {top: 684, bottom: 830, left: 12, right: 378};
  const resized = historySheetBounds(mobile, {width: 390, height: 844}, 84, openness);
  assert.equal(resized.closedTop, mobile.top);
  assert.equal(resized.left, 12);
  assert.equal(resized.right, 12);
  assert.equal(resized.bottom, 16);
  assert(Math.abs((mobile.top - resized.top) / (mobile.top - 84) - openness) < 1e-12);
  const warning = historySheetBounds({...mobile, top: 659}, {width: 390, height: 844}, 157, 1);
  assert.equal(warning.top, 157);
  assert.equal(historySheetBounds(mobile, {width: 390, height: 844}, 157, 0).top, mobile.top);
});
