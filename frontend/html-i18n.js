// Build/dev only: Turkish first-paint text comes from the same dictionary as the interface.
// Explicit slots also preserve text after icons and before nested controls. This module never
// enters the browser graph; escape both text and quoted attributes before Vite parses the HTML.
import { trPage } from './src/i18n/tr.js';

const entities = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
export function fillHtml(source, page = trPage) {
  return source.replace(/\{\{i18n:([^{}]+)\}\}/g, (_, key) => {
    const value = Object.hasOwn(page, key) ? page[key] : undefined;
    if (typeof value !== 'string' || !value.trim()) throw new Error(`HTML i18n: missing text for ${key}`);
    return value.replace(/[&<>"']/g, character => entities[character]);
  });
}

export function htmlI18n() {
  return {
    name: 'html-i18n',
    transformIndexHtml: { order: 'pre', handler: html => fillHtml(html) },
  };
}
