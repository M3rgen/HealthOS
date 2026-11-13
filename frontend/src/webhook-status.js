import { setupDialog } from './accessibility.js';
import { fullDateTime } from './dates.js';
import { $, setText } from './dom.js';
import { morphChildren } from './dom-morph.js';
import { num } from './format.js';
import { t } from './i18n/index.js';

/**
 * @typedef {{state: string, never_received: boolean, warning: boolean, since?: string | null, threshold_seconds: number}} Silence
 * @typedef {{last_notification_at?: string | null, silence: Silence, last_error?: {code: string, stage: string, at: string} | null,
 *   counters: {since?: string | null, valid: number, invalid: number, expired: number, reasons: Record<string, number>}}} WebhookStatus
 * @typedef {(value: string | null | undefined) => string} DateText
 */
const strings = t.webhook;
const rows = strings.rows;
/** @type {Record<string, string>} */
const reasons = strings.reasons;

/** @param {string} text */
function message(text) {
  setText($('webhook-status-summary'), text);
  setText($('webhook-status-message'), text);
}

/** @param {string} label @param {string} value */
function row(label, value) {
  const group = document.createElement('div');
  group.dataset.key = label;
  const term = document.createElement('dt');
  const description = document.createElement('dd');
  term.textContent = label;
  description.textContent = value;
  group.append(term, description);
  return group;
}

/** @param {WebhookStatus} status @param {DateText} date */
function summaryText(status, date) {
  const { silence } = status;
  /** @type {Record<string, string>} */
  const summaries = {
    unconfigured: strings.summaries.unconfigured,
    never_received: strings.summaries.neverReceived,
    receiving: strings.summaries.receiving(date(status.last_notification_at)),
    silent: silence.never_received ? strings.summaries.silentNever : strings.summaries.silent,
  };
  return summaries[silence.state] ?? strings.unknownState;
}

/** @param {WebhookStatus} status @param {DateText} date */
function lastErrorText(status, date) {
  const error = status.last_error;
  if (!error) return rows.noError;
  return `${reasons[error.code] ?? rows.unknownError} · ${error.stage === 'processing' ? rows.processing : rows.verification} · ${date(error.at)}`;
}

/** @param {WebhookStatus} status @param {DateText} date */
function detailRows(status, date) {
  const { silence, counters } = status;
  return [
    row(rows.lastNotification, date(status.last_notification_at)),
    row(rows.silenceSince, date(silence.since)),
    row(rows.threshold, rows.thresholdDays(num(silence.threshold_seconds / 86400, 1))),
    row(rows.subscription, rows.subscriptionValue),
    row(rows.counterScope, rows.counterScopeValue(date(counters.since))),
    row(rows.counterMeaning, rows.counterMeaningValue),
    row(rows.counts, `${num(counters.valid)} / ${num(counters.invalid)} / ${num(counters.expired)}`),
    row(rows.expired, rows.expiredValue),
    ...Object.entries(reasons).map(([code, label]) => row(label, num(counters.reasons[code] ?? 0))),
    row(rows.lastError, lastErrorText(status, date)),
    row(rows.delivery, rows.deliveryValue),
  ];
}

/** Ready nodes only: diagnostics never pass through an HTML parser. @param {WebhookStatus} status @param {DateText} date */
function webhookDetailsFragment(status, date) {
  const fragment = document.createDocumentFragment();
  fragment.append(...detailRows(status, date));
  return fragment;
}

/** @param {WebhookStatus} status @param {DateText} date */
function paintStatus(status, date) {
  message(summaryText(status, date));
  $('webhook-status-summary').classList.toggle('warning', status.silence.warning);
  morphChildren($('webhook-status-details'), webhookDetailsFragment(status, date));
}

/** @param {boolean} busy */
function setBusy(busy) {
  const details = $('webhook-status-details'), retry = $('webhook-status-retry');
  details.setAttribute('aria-busy', String(busy));
  if (busy) retry.setAttribute('aria-disabled', 'true');
  else retry.removeAttribute('aria-disabled');
}

/** @param {{request: (url: string) => Promise<WebhookStatus>, getZone: () => string}} parts */
export function setupWebhookStatus({ request, getZone }) {
  const dialog = $('webhook-status-dialog');
  const modal = setupDialog(dialog);
  let loading = false;
  // After a first good answer, a refresh stays silent until its result differs.
  let painted = false;
  /** @type {DateText} */
  const date = value => value ? fullDateTime(value, getZone()) : strings.none;

  function showLoading() {
    message(strings.loading);
    $('webhook-status-summary').classList.remove('warning');
    $('webhook-status-details').replaceChildren();
    setBusy(true);
  }

  async function refresh() {
    if (loading) return;
    loading = true;
    if (!painted) showLoading();
    try {
      paintStatus(await request('/api/webhooks/status'), date);
      painted = true;
    } catch {
      painted = false;
      message(strings.failed);
      $('webhook-status-details').replaceChildren();
    } finally {
      loading = false;
      setBusy(false);
    }
  }
  $('webhook-status-open').addEventListener('click', () => modal.open());
  $('webhook-status-close').addEventListener('click', () => dialog.close());
  $('webhook-status-retry').addEventListener('click', refresh);
  return { refresh };
}
