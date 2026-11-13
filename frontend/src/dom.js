// The one DOM lookup of the interface. Page markup is fixed in index.html, so an id either exists
// or the page is broken at build time; dom-ids.d.ts (generated) maps each id to its element type.
/**
 * @template {string} Id
 * @param {Id} id
 * @returns {Id extends keyof import('./dom-ids').ElementsById ? import('./dom-ids').ElementsById[Id] : HTMLElement}
 */
export const $ = id => /** @type {Id extends keyof import('./dom-ids').ElementsById ? import('./dom-ids').ElementsById[Id] : HTMLElement} */
  (/** @type {unknown} */ (document.getElementById(id)));
/**
 * Elements matching a selector, as an array.
 * @template {HTMLElement} [T=HTMLElement]
 * @param {string} selector
 * @param {ParentNode} [root]
 * @returns {T[]}
 */
export const $$ = (selector, root = document) => /** @type {T[]} */ ([...root.querySelectorAll(selector)]);
/** The element nearest an event's target that matches a selector, or null. @type {(event: Event, selector: string) => HTMLElement | null} */
export const closest = (event, selector) => /** @type {Element} */ (event.target).closest(selector);

/** Writes only a changed text: a live region announces every mutation, even an identical one. @type {(element: Element, text: string) => void} */
export function setText(element, text) {
  if (element.textContent !== text) element.textContent = text;
}
/** Writes only a changed attribute, for the same reason. @type {(element: Element, name: string, value: string) => void} */
export function setAttr(element, name, value) {
  if (element.getAttribute(name) !== value) element.setAttribute(name, value);
}
