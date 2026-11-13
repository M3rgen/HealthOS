import { civilRange } from './dates.js';
import { num } from './format.js';
import { t } from './i18n/index.js';

const { states, actions, counts } = t.transfer;

/**
 * @template {keyof HTMLElementTagNameMap} K
 * @param {K} tag @param {string} text @param {Element} parent @param {string} [className]
 */
function node(tag, text, parent, className) {
  const element = document.createElement(tag);
  if (text) element.textContent = text;
  if (className) element.className = className;
  parent.append(element);
  return element;
}

// The API's yyyy-mm-dd bounds (or null) in the interface language.
/** @param {{first?: string|null, last?: string|null} | null | undefined} value */
function range(value) {
  if (!value) return t.transfer.none;
  const dated = [value.first, value.last].every(day => /^\d{4}-\d{2}-\d{2}$/.test(day ?? ''));
  return dated ? civilRange(/** @type {string} */ (value.first), /** @type {string} */ (value.last)) : `${value.first} – ${value.last}`;
}

/** @param {Record<string, number | undefined>} values */
function countText(values) {
  return Object.entries(counts).map(([key, label]) => t.transfer.count(label, num(values[key] ?? 0))).join(' · ');
}

// Kept beside this turn's question and answers; never stored or reused by a new socket.
/** @param {HTMLElement} container */
function buildView(container) {
  const details = node('details', '', container, 'chat-transfer');
  const title = node('summary', '', details);
  const body = node('div', '', details, 'chat-transfer-body');
  body.setAttribute('data-scroll-focus', '');
  body.setAttribute('role', 'region');
  body.setAttribute('aria-label', t.transfer.region);
  const totals = node('p', '', body), notice = node('p', '', body), failure = node('p', '', body);
  node('p', t.transfer.fieldLegend, body);
  return { details, title, body, totals, notice, failure };
}

/** @param {HTMLElement} container */
export function createTransfer(container) {
  /** @type {import('./ui-types').TransferReceipt | null} the latest health_transfer frame */
  let receipt = null;
  /** @type {{details: HTMLElement, title: HTMLElement, body: HTMLElement, totals: HTMLElement, notice: HTMLElement, failure: HTMLElement} | null} */
  let view = null;
  let shownCalls = 0;
  /** @param {import('./ui-types').TransferReceipt} event */
  function update(event) {
    if (receipt && receipt.turn !== event.turn) return;
    receipt = event;
    view ??= buildView(container);
    const { title, body, totals, notice, failure } = view;
    title.textContent = t.transfer.title(/** @type {Record<string, string>} */ (states)[event.status] ?? states.failed);
    totals.textContent = countText(event.counts);
    const healthSent = Object.keys(counts).some(key => key !== 'metadata_items' && event.counts[key] > 0);
    notice.textContent = healthSent ? t.transfer.healthSent :
      event.calls.length ? t.transfer.metadataOnly : t.transfer.nothingSent;
    failure.hidden = !event.failed_sends;
    failure.textContent = t.transfer.failedSends(num(event.failed_sends));
    event.calls.slice(shownCalls).forEach((/** @type {import('./ui-types').TransferCall} */ call, /** @type {number} */ offset) => {
      const index = shownCalls + offset;
      node('h3', `${index + 1}. ${/** @type {Record<string, string>} */ (actions)[call.action] ?? actions.invalid}${call.outcome === 'refused' ? t.transfer.refused : ''}`, body);
      node('p', countText(call.counts), body);
      node('p', t.transfer.window(range(call.requested), call.timezone), body);
      node('p', t.transfer.dataDates(range(call.data_dates)), body);
      if (call.profile_dates) node('p', t.transfer.profileDates(range(call.profile_dates)), body);
      if (call.metadata_dates) node('p', t.transfer.metadataDates(range(call.metadata_dates)), body);
      if (call.source_timezones.length) node('p', t.transfer.sourceTimezones(call.source_timezones.join(', ')), body);
      const fields = node('details', '', body, 'chat-transfer-fields');
      node('summary', t.transfer.fields(num(call.fields.length)), fields);
      const list = node('ul', '', fields);
      list.setAttribute('data-scroll-focus', '');
      list.setAttribute('role', 'region');
      list.setAttribute('aria-label', t.transfer.fieldsRegion(index + 1));
      call.fields.forEach((/** @type {string} */ field) => node('li', field, list));
    });
    shownCalls = event.calls.length;
  }
  return {
    update,
    disconnect() {
      if (receipt?.status === 'active') update({...receipt, status: 'disconnected'});
    },
  };
}
