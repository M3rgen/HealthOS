import assert from 'node:assert/strict';
import { test } from 'node:test';
import { SIDE_MARGIN, historySheetBounds } from '../../frontend/src/history-sheet.js';

test('beside the cards the drag widens the panel leftwards in proportion and keeps its height', () => {
  const closed = {top: 53, bottom: 382, left: 455, right: 836};
  const half = historySheetBounds(closed, {width: 844, height: 390}, 52, 0.5, true);
  assert.equal(half.top, closed.top);
  assert.equal(half.bottom, 390 - closed.bottom);
  assert.equal(half.right, 844 - closed.right);
  assert.equal(half.left, closed.left - (closed.left - SIDE_MARGIN) / 2);
  assert.equal(historySheetBounds(closed, {width: 844, height: 390}, 52, 0, true).left, closed.left);
  assert.equal(historySheetBounds(closed, {width: 844, height: 390}, 52, 1, true).left, SIDE_MARGIN);
});

test('the default bounds still drive the top edge', () => {
  const closed = {top: 504, bottom: 776, left: 12, right: 1268};
  const bounds = historySheetBounds(closed, {width: 1280, height: 800}, 90, 0.5);
  assert.equal(bounds.left, closed.left);
  assert.equal(bounds.top, closed.top - (closed.top - 90) / 2);
});
