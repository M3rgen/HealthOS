// Plan formats dates and numbers with the interface's own helpers (dates.js, locale.js), handed
// over by main.js. Importing them from this lazy chunk would split them out of the first-load
// chunk into one of their own and cost first-load JavaScript, which keeps 800 B of headroom.
/** @typedef {{civilDate: typeof import('./dates.js').civilDate, civilParts: typeof import('./dates.js').civilParts,
 * shiftDay: typeof import('./dates.js').shiftDay, number: typeof import('./locale.js').number}} PlanFormat */
export const format = /** @type {PlanFormat} */ ({});
/** @param {PlanFormat} helpers */
export function useFormat(helpers) {
  Object.assign(format, helpers);
}
