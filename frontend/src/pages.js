// The top menu: one page at a time, its data fetched on opening and its name in the URL, so Back,
// Forward and a reload keep the page. The fragment starts with a slash on purpose: a plain
// #sports would match <section id="sports"> and the browser would scroll the document to it.
import { focusVisible } from './accessibility.js';
import { $, $$ } from './dom.js';
import { t } from './i18n/index.js';
import { state } from './state.js';

const HOME = 'overview';
export const PAGES = [HOME, 'measurements', 'system', 'sports'];
const PREFIX = '#/';

/** The page a URL fragment names, or null when it names none. @param {string} hash */
export function pageFromHash(hash) {
  const name = hash.startsWith(PREFIX) ? hash.slice(PREFIX.length) : '';
  return PAGES.includes(name) ? name : null;
}

/** @param {string} page */
const urlFor = page => (page === HOME ? `${location.pathname}${location.search}` : `${PREFIX}${page}`);

/** @param {string} page */
const menuButton = page => document.querySelector(`[data-page="${page}"]`);

/** @param {string} page */
function show(page) {
  $('date-calendar').hidePopover();
  state.activePage = page;
  $$('.page').forEach(el => (el.hidden = el.id !== page));
  document.title = t.pages.title(/** @type {Record<string, string>} */ (/** @type {unknown} */ (t.pages))[page]);
  $$('[data-page]').forEach(el => {
    el.classList.toggle('active', el.dataset.page === page);
    if (el.dataset.page === page) el.setAttribute('aria-current', 'page');
    else el.removeAttribute('aria-current');
  });
}

/** After Back or Forward: focus moves only when it would otherwise be lost in a hidden page. */
function keepFocus() {
  const current = document.activeElement;
  if (!current || current === document.body || current.closest('.page[hidden]')) focusVisible(menuButton(state.activePage));
}

/**
 * @param {object} options
 * @param {() => void} options.onChange
 * @param {Record<string, () => unknown>} options.loaders run each time a page opens
 * @param {Record<string, () => unknown>} [options.firstLoaders] run when the URL opens a page on load
 */
export function setupPages({ onChange, loaders, firstLoaders = loaders }) {
  history.scrollRestoration = 'manual';

  /** @param {string} page */
  function open(page) {
    if (page !== state.activePage) history.pushState(null, '', urlFor(page));
    show(page);
    onChange();
    focusVisible(menuButton(page));
    loaders[page]?.();
  }

  function follow() {
    const page = pageFromHash(location.hash) ?? HOME;
    if (page === state.activePage) return;
    // The dialog belongs to the page being left.
    /** @type {HTMLDialogElement[]} */ ($$('dialog[open]')).forEach(dialog => dialog.close());
    show(page);
    onChange();
    keepFocus();
    loaders[page]?.();
  }

  $$('[data-page]').forEach(el => el.addEventListener('click', () => open(/** @type {string} */ (el.dataset.page))));
  window.addEventListener('popstate', follow);

  const first = pageFromHash(location.hash) ?? HOME;
  if (location.hash && first === HOME) history.replaceState(null, '', urlFor(HOME));
  show(first);
  onChange();
  firstLoaders[first]?.();
}
