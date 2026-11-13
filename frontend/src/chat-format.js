import { marked } from 'marked';
import DOMPurify from 'dompurify';

/**
 * Model output can be steered by text from a data source, and a link is a way out: one click on
 * https://elsewhere/?hrv=41 hands that value to another site. Only a link that stays on this site
 * keeps its target (opened in a new tab, without a handle on this one); any other drops `href`
 * and remains as plain text.
 * @param {Element} node @param {string} [origin] this site's origin
 */
export function limitLinks(node, origin = globalThis.location?.origin) {
  if (node.tagName !== 'A' || !node.hasAttribute('href')) return;
  if (!isOnSite(/** @type {string} */ (node.getAttribute('href')), origin)) {
    node.removeAttribute('href');
    return;
  }
  node.setAttribute('target', '_blank');
  node.setAttribute('rel', 'noopener noreferrer');
}
/** @param {string} href @param {string} origin */
function isOnSite(href, origin) {
  try {
    return new URL(href, origin).origin === origin;
  } catch {
    return false;
  }
}
// Runs after the allow-list below has removed every other attribute. Outside a browser DOMPurify
// has no API at all (isSupported is false); node tests still import this module.
if (DOMPurify.isSupported) DOMPurify.addHook('afterSanitizeAttributes', node => limitLinks(node));

// Model output is untrusted. Allow text formatting, never embedded media or controls.
/** @param {string} text */
export function formatAssistantMessage(text) {
  return DOMPurify.sanitize(/** @type {string} */ (marked.parse(text, {gfm: true, breaks: true})), {
    ALLOWED_TAGS: ['p', 'br', 'strong', 'em', 'del', 'ul', 'ol', 'li', 'blockquote',
      'pre', 'code', 'a', 'table', 'thead', 'tbody', 'tr', 'th', 'td',
      'h1', 'h2', 'h3', 'h4', 'h5', 'h6'],
    ALLOWED_ATTR: ['href', 'title', 'start'],
    ALLOW_DATA_ATTR: false,
    ALLOW_ARIA_ATTR: false,
  });
}
