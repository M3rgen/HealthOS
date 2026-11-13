// Read current grid bounds rather than retaining dimensions from before a resize.
// Beside the cards (landscape phones) the panel already fills the column height, so the drag widens
// it leftwards over the cards instead of raising its top edge.
export const SIDE_MARGIN = 8;
/**
 * @param {{top: number, left: number, right: number, bottom: number}} closed the placeholder's box
 * @param {{width: number, height: number}} viewport
 * @param {number} upper highest allowed top edge
 * @param {number} openness 0 closed … 1 fully open
 * @param {boolean} [sideways]
 */
export function historySheetBounds(closed, { width, height }, upper, openness, sideways = false) {
  const limit = Math.min(upper, closed.top);
  return {
    closedTop: closed.top,
    closedLeft: closed.left,
    sideways,
    top: sideways ? closed.top : closed.top - (closed.top - limit) * openness,
    bottom: sideways ? height - closed.bottom : Math.max(16, height - closed.bottom),
    left: sideways ? closed.left - (closed.left - SIDE_MARGIN) * openness : closed.left,
    right: width - closed.right,
  };
}
