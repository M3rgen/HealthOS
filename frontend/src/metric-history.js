// Open and focus the dialog immediately; fetch its rendering code only on first interaction.
import { setupDialog, focusVisible } from './accessibility.js';
import { $, $$ } from './dom.js';
import { t } from './i18n/index.js';

/** @typedef {import('./metric-history-content.js').Metric} Metric */
const text = t.metricHistory;
export const metrics = {
  steps: {title: text.titles.steps, average: text.averages.steps, color: 'var(--step-chart)'},
  sleep: {title: text.titles.sleep, average: text.averages.sleep, color: 'var(--sleep-light)'},
  heart: {title: text.titles.heart, average: text.averages.heart, color: 'var(--heart-chart)'},
};

/** The dialog while a page is on its way; focus inside the emptied parts moves to the metric's tab. @param {Metric} metric @param {HTMLDialogElement} dialog */
export function showLoading(metric, dialog) {
  const focused = document.activeElement;
  if (['metric-history-content', 'metric-history-status', 'metric-history-pages'].some(id => $(id).contains(focused)))
    focusVisible(dialog.querySelector(`[data-history-tab="${metric}"]`));
  $('metric-history-title').textContent = metrics[metric].title;
  dialog.style.setProperty('--metric-accent', metrics[metric].color);
  $$('[data-history-tab]').forEach(el => el.setAttribute('aria-pressed', String(el.dataset.historyTab === metric)));
  $('metric-history-content').hidden = true;
  $('metric-history-status').hidden = false;
  $('metric-history-status').textContent = text.loading;
  $('metric-history-pages').replaceChildren();
  $('metric-history-page-note').textContent = '';
  $('metric-history-range').textContent = '';
}

/** @param {() => string} getTimezone */
export function setupMetricHistory(getTimezone) {
  const dialog = $('metric-history-dialog');
  const modal = setupDialog(dialog);
  let intent = 0;
  /** @type {Promise<ReturnType<typeof import('./metric-history-content.js').setupMetricHistoryContent>> | undefined} */
  let content;
  /** @param {Metric} metric @param {Element} trigger */
  async function open(metric, trigger) {
    const current = ++intent;
    modal.open({ trigger });
    document.body.classList.add('metric-history-open');
    showLoading(metric, dialog);
    try {
      content ??= import('./metric-history-content.js').then(module => module.setupMetricHistoryContent(getTimezone));
      const history = await content;
      if (current === intent && dialog.open) history.load(metric);
    } catch {
      content = undefined;
      if (current !== intent || !dialog.open) return;
      $('metric-history-status').textContent = text.loadFailed;
      const retry = document.createElement('button');
      retry.type = 'button'; retry.className = 'button outline'; retry.textContent = text.retry;
      retry.addEventListener('click', () => open(metric, trigger));
      $('metric-history-status').append(' ', retry);
    }
  }
  $$('[data-history-metric]').forEach(button => button.addEventListener('click', () =>
    open(/** @type {Metric} */ (button.dataset.historyMetric), button)));
  $$('[data-history-tab]').forEach(button => button.addEventListener('click', () =>
    open(/** @type {Metric} */ (button.dataset.historyTab), button)));
  $('metric-history-close').addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => {
    intent++;
    document.body.classList.remove('metric-history-open');
  });
}
