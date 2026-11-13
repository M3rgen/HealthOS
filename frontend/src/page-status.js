// What the data modules know about the two pages with an empty or error state. They publish facts
// here; page-states.js reads them, so a state never has to be guessed from the rendered text.
//   loadFailed  the last request for this page's data failed
//   rows        how many days (overview) or measurements are shown; null until the first answer
//   hasValues   at least one shown row holds a measured value
/** @typedef {{loadFailed: boolean, rows: number | null, hasValues: boolean}} PageStatus */
/** @typedef {'overview'|'measurements'} StatusPage */

/** @type {Record<StatusPage, PageStatus>} */
const statuses = {
  overview: { loadFailed: false, rows: null, hasValues: false },
  measurements: { loadFailed: false, rows: null, hasValues: false },
};
/** @type {Set<() => void>} */
const listeners = new Set();

/** @param {StatusPage} page */
export const pageStatus = page => statuses[page];

/** @param {StatusPage} page @param {Partial<PageStatus>} changes */
export function setPageStatus(page, changes) {
  Object.assign(statuses[page], changes);
  listeners.forEach(listener => listener());
}

/** @param {() => void} listener */
export function onPageStatus(listener) {
  listeners.add(listener);
}
