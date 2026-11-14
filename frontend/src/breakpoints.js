// Shared width thresholds, in em so browser font preferences move CSS and JS together.
// css-breakpoints.js expands bp(name) in media queries before Vite compiles the CSS.
export const widths = /** @type {const} */ ({
  w319: '19.9375em',
  w320: '20em',
  w360: '22.5em',
  w480: '30em',
  w540: '33.75em',
  w567: '35.4375em',
  w568: '35.5em',
  w704: '44em',
  w760: '47.5em',
  w900: '56.25em',
  w901: '56.3125em',
  w1150: '71.875em',
  w1400: '87.5em',
});

export const heights = /** @type {const} */ ({
  h480: '30em',
  h560: '35em',
});

// Every query condition has a stylesheet counterpart; Umay shares the 900 px layout boundary.
export const queries = /** @type {const} */ ({
  umayModal: `(max-width: ${widths.w900}), (max-height: ${heights.h480})`,
  besideCards: `(max-height: ${heights.h480}) and (min-width: ${widths.w568})`,
  shortScreen: `(max-height: ${heights.h480})`,
  overviewTabs: `(max-width: ${widths.w900}), (max-height: ${heights.h480})`,
  systemTabs: `(max-width: ${widths.w900}), (max-height: ${heights.h560})`,
});
