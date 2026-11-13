// One panel at a time behind a tab bar, on screens too small to show them all. The panels stay
// in the DOM either way, so the data modules keep filling them in; only `hidden` changes.

/**
 * Changing the browser's font size moves every `em` breakpoint, and the stylesheet follows at once;
 * a MediaQueryList does not fire `change` for it, so the tab state would stay behind the layout.
 * A 1rem box resizes with the font, which is the signal.
 * @param {() => void} changed
 */
function watchFontSize(changed) {
  if (typeof ResizeObserver !== 'function') return;
  const probe = document.createElement('span');
  probe.setAttribute('aria-hidden', 'true');
  probe.style.cssText = 'position:fixed;top:0;left:0;width:1rem;height:0;visibility:hidden;pointer-events:none';
  document.body.append(probe);
  new ResizeObserver(changed).observe(probe);
}

/**
 * @typedef {{key: string, label: string, panel: HTMLElement}} PanelTab
 * @param {{host: HTMLElement, prefix: string, listLabel: string, items: PanelTab[], media: MediaQueryList, initial: string, onChange?: (key: string) => void}} options
 *   `host` is the panels' parent; it receives the tab list and a `data-tabbed` attribute that the stylesheet follows.
 */
export function createPanelTabs({ host, prefix, listLabel, items, media, initial, onChange = () => {} }) {
  const list = document.createElement('div');
  list.className = `${prefix}-tabs panel-tabs`;
  list.setAttribute('role', 'tablist');
  list.setAttribute('aria-label', listLabel);
  const tabs = items.map(({ key, label, panel }) => {
    panel.id ||= `${prefix}-card-${key}`;
    const tab = document.createElement('button');
    tab.type = 'button';
    tab.id = `${prefix}-tab-${key}`;
    tab.className = `${prefix}-tab panel-tab`;
    tab.dataset.tab = key;
    tab.setAttribute('role', 'tab');
    tab.setAttribute('aria-controls', panel.id);
    tab.textContent = label;
    list.append(tab);
    // The wide layout keeps the panel's own role and name; both come back when the tabs leave.
    return { key, tab, panel, role: panel.getAttribute('role'), label: panel.getAttribute('aria-labelledby') };
  });
  host.prepend(list);
  let active = initial;

  /** @param {HTMLElement} panel @param {string} name @param {string | null} value */
  function setOrRemove(panel, name, value) {
    if (value === null) panel.removeAttribute(name);
    else panel.setAttribute(name, value);
  }

  function apply() {
    const tabbed = media.matches;
    host.toggleAttribute('data-tabbed', tabbed);
    list.hidden = !tabbed;
    for (const { key, tab, panel, role, label } of tabs) {
      const selected = key === active;
      tab.setAttribute('aria-selected', String(selected));
      tab.tabIndex = selected ? 0 : -1;
      panel.hidden = tabbed && !selected;
      setOrRemove(panel, 'role', tabbed ? 'tabpanel' : role);
      setOrRemove(panel, 'aria-labelledby', tabbed ? tab.id : label);
    }
  }

  /** @param {string} key @param {boolean} [focus] */
  function select(key, focus = false) {
    active = key;
    apply();
    onChange(key);
    if (focus) tabs.find(item => item.key === key)?.tab.focus();
  }

  list.addEventListener('click', event => {
    const tab = /** @type {Element} */ (event.target).closest('[role="tab"]');
    if (tab instanceof HTMLElement && tab.dataset.tab) select(tab.dataset.tab);
  });
  // Arrow keys move between tabs and show their panel (automatic activation); Home/End jump.
  list.addEventListener('keydown', event => {
    const index = tabs.findIndex(item => item.key === active);
    const next = /** @type {Record<string, number>} */ ({ ArrowRight: index + 1, ArrowLeft: index - 1, Home: 0, End: tabs.length - 1 })[event.key];
    if (next === undefined) return;
    event.preventDefault();
    select(tabs[(next + tabs.length) % tabs.length].key, true);
  });
  media.addEventListener('change', apply);
  watchFontSize(apply);
  apply();
  return { select };
}
