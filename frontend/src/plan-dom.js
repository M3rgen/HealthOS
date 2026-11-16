// Plan strings enter the document only through textContent: no HTML or Markdown interpretation.
import { text } from './i18n/plan.js';
import { format } from './plan-format.js';
import { priceTotals } from './plan-model.js';

/** @template {keyof HTMLElementTagNameMap} K
 * @param {K} tag @param {string} [content] @param {string} [className] */
export function el(tag, content = '', className = '') {
  const node = document.createElement(tag);
  node.textContent = content;
  node.className = className;
  return node;
}
/** @param {string} label @param {() => void} action */
export function button(label, action) {
  const node = el('button', label, 'button outline');
  node.type = 'button';
  node.onclick = action;
  return node;
}
/** @param {string} title */
export function card(title) {
  const node = el('article', '', 'panel plan-card');
  node.append(el('h3', title));
  return node;
}
/** @param {HTMLElement} parent @param {string[]} [items] @param {string} [title] */
export function list(parent, items = [], title = '') {
  if (!items.length) return;
  if (title) parent.append(el('h3', title));
  const ul = el('ul');
  for (const item of items) ul.append(el('li', item));
  parent.append(ul);
}
/** @param {number} value */
export const number = value => format.number(value, 2);
/** @param {import('./plan-types').ShoppingItem[]} items */
export function prices(items) {
  return Object.entries(priceTotals(items)).map(([currency, value]) => `${number(value)} ${currency}`).join(' · ');
}
/** @param {HTMLElement} parent @param {import('./plan-types').ShoppingItem[]} items */
export function estimated(parent, items) {
  const total = prices(items);
  if (total) parent.append(el('p', `${text.estimated}: ${total}`, 'plan-total'));
}
/** @param {string} title @param {HTMLElement} detail */
export function expandable(title, detail) {
  detail.hidden = true;
  detail.id = `plan-detail-${++detailId}`;
  const toggle = button(title, () => {
    detail.hidden = !detail.hidden;
    toggle.setAttribute('aria-expanded', String(!detail.hidden));
  });
  toggle.setAttribute('aria-expanded', 'false');
  toggle.setAttribute('aria-controls', detail.id);
  return toggle;
}
/** @param {string} title @param {() => HTMLElement} content */
export function disclosure(title, content) {
  const row = el('div', '', 'plan-disclosure');
  const detail = content();
  row.append(expandable(title, detail), detail);
  return row;
}
let detailId = 0;
/** A caption that repeats the visible card or row title stays for screen readers only.
 * @param {string} title @param {string[]} headings @param {string[][]} rows @param {boolean} [titled] */
export function table(title, headings, rows, titled = false) {
  const wrap = el('div', '', 'plan-table');
  wrap.dataset.scrollFocus = '';
  wrap.setAttribute('role', 'region');
  wrap.setAttribute('aria-label', title);
  const node = el('table');
  node.append(el('caption', title, titled ? 'sr-only' : ''));
  const head = el('thead'), header = el('tr'), body = el('tbody');
  for (const heading of headings) {
    const th = el('th', heading);
    th.scope = 'col';
    header.append(th);
  }
  head.append(header);
  for (const values of rows) {
    const row = el('tr');
    for (const value of values) row.append(el('td', value));
    body.append(row);
  }
  node.append(head, body);
  wrap.append(node);
  return wrap;
}
