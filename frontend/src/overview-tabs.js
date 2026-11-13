// Narrow or very short screens (≤900 px wide, ≤480 px tall): the overview's cards become tabs, one card at a time, instead of a
// column that scrolls inside the page next to the scrolling history table. Wider screens show
// every card and no tab bar. A hidden card's 3D model pauses (body.js skips frames without a box).
import { queries } from './breakpoints.js';
import { t } from './i18n/index.js';
import { createPanelTabs } from './panel-tabs.js';

export const NARROW = queries.overviewTabs;
const CARDS = /** @type {const} */ ([
  ['profile', '.profile-panel'],
  ['connection', '.connection'],
  ['steps', '.steps-card'],
  ['sleep', '.sleep-card'],
  ['heart', '.heart-card'],
]);

/** @param {HTMLElement} grid @param {{media?: MediaQueryList}} [options] */
export function setupOverviewTabs(grid, { media = matchMedia(NARROW) } = {}) {
  return createPanelTabs({
    // A phone opens on its daily steps; selecting Profile reveals and loads the body.
    // Wide screens keep all cards visible and retain their existing model loading.
    host: grid, prefix: 'overview', listLabel: t.overviewTabs.label, media, initial: media.matches ? 'steps' : 'profile',
    items: CARDS.map(([key, selector]) => ({
      key, label: t.overviewTabs[key], panel: /** @type {HTMLElement} */ (grid.querySelector(selector)),
    })),
  });
}
