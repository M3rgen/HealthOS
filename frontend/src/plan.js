import './plan.css';
import { button, el } from './plan-dom.js';
import { sections } from './plan-model.js';
import { renderAssumptions, renderBatch, renderDay, renderNutrition, renderToday } from './plan-nutrition.js';
import { renderShopping } from './plan-shopping.js';
import { createPlanState } from './plan-state.js';
import { useFormat } from './plan-format.js';
import { setPlanLanguage, text } from './i18n/plan.js';
import { renderMeasurements, renderTraining } from './plan-training.js';

const renderers = { today: renderToday, nutrition: renderNutrition, batch: renderBatch, shopping: renderShopping,
  training: renderTraining, measurements: renderMeasurements, assumptions: renderAssumptions };
/** @typedef {keyof typeof renderers} Section */
class PlanPage {
  /** @param {PlanOptions} options */
  constructor({ notice, request, getToday }) {
    setPlanLanguage(document.documentElement.lang);
    this.notice = notice;
    this.request = request;
    this.getToday = getToday;
    this.root = /** @type {HTMLElement} */ (document.getElementById('plan'));
    this.nav = el('nav', '', 'plan-nav');
    this.nav.setAttribute('aria-label', text.sections);
    this.content = el('div', '', 'plan-content');
    this.content.dataset.scrollFocus = '';
    this.content.setAttribute('role', 'region');
    this.content.setAttribute('aria-label', 'Plan');
    this.status = el('p', '', 'fine-print plan-save-status');
    this.status.setAttribute('role', 'status');
    this.heading = el('header', '', 'plan-heading');
    this.heading.dataset.scrollFocus = '';
    this.heading.append(el('h1', 'Plan'));
    this.root.replaceChildren(this.heading, this.nav, this.content, this.status);
    /** @type {import('./plan-types').Plan | null} */
    this.plan = null;
    /** @type {Section | undefined} */
    this.selected = undefined;
    this.loading = false;
    this.version = 0;
    this.chosenDate = '';
    this.lastStart = '';
    this.revision = 0;
    /** @type {import('./plan-types').PlanActions} */
    this.actions = {
      today: getToday(), date: getToday(), state: { start_date: null, checked_item_ids: [] },
      selectDate: date => { this.chosenDate = date; this.paint(); },
      changeState: patch => { this.revision++; this.store.change(patch); },
      measure: () => document.getElementById('measurements-add')?.click(),
    };
    this.store = createPlanState({
      initial: this.actions.state,
      write: value => request('/api/plan/state', {
        method: 'PUT', headers: { 'Content-Type': 'application/json', 'X-HealthOS-Request': '1' }, body: JSON.stringify(value),
      }),
      changed: (value, saving, succeeded) => {
        this.actions.state = value;
        const message = saving ? text.saving : succeeded ? text.saved : '';
        if (this.status.textContent !== message) this.status.textContent = message;
        this.syncControls();
      },
      failed: () => notice(text.saveFailed, true, 'plan-state'),
    });
  }
  paint() {
    if (!this.plan || !this.selected) return;
    const previous = document.activeElement;
    const focused = previous?.id;
    const rowIndex = [...this.content.querySelectorAll('button')].indexOf(/** @type {HTMLButtonElement} */ (previous));
    this.actions.today = this.getToday();
    this.actions.date = this.chosenDate || this.actions.today;
    if (this.selected === 'today' && this.content.querySelector('#plan-start')) this.paintDay();
    else {
      this.content.replaceChildren(el('h2', text[this.selected]));
      renderers[this.selected](this.content, this.plan, this.actions);
    }
    this.restoreFocus(focused, rowIndex);
    this.markSection();
  }
  /** @param {string | undefined} focused @param {number} rowIndex */
  restoreFocus(focused, rowIndex) {
    // focus() on a date field that kept focus would reset the segment being typed.
    const target = focused?.startsWith('plan-') ? document.getElementById(focused) : null;
    if (target) { if (target !== document.activeElement) target.focus({ preventScroll: true }); }
    else if (rowIndex >= 0) this.content.querySelectorAll('button')[rowIndex]?.focus({ preventScroll: true });
  }
  markSection() {
    this.nav.querySelectorAll('button').forEach(node => {
      const current = node.dataset.section === this.selected;
      // .outline comes later in the cascade than .primary, so the current section drops it.
      node.classList.toggle('primary', current);
      node.classList.toggle('outline', !current);
      if (current) node.setAttribute('aria-current', 'true');
      else node.removeAttribute('aria-current');
    });
  }
  /** Chrome commits a date field on every typed segment. Rebuilding it would drop the next
   * segment and save a wrong date: keep both fields, update their values, redraw what follows. */
  paintDay() {
    const fields = [...this.content.children];
    const last = fields.findIndex(node => node.querySelector('#plan-start'));
    for (const node of fields.slice(last + 1)) node.remove();
    const values = { 'plan-day': this.actions.date, 'plan-start': this.actions.state.start_date ?? '' };
    for (const [id, value] of Object.entries(values)) {
      const input = /** @type {HTMLInputElement} */ (document.getElementById(id));
      if (input.value !== value) input.value = value;
    }
    renderDay(this.content, /** @type {import('./plan-types').Plan} */ (this.plan), this.actions);
  }
  syncControls() {
    this.content.querySelectorAll('input[data-item-id]').forEach(node => {
      const input = /** @type {HTMLInputElement} */ (node);
      input.checked = this.actions.state.checked_item_ids.includes(input.dataset.itemId ?? '');
    });
    const next = this.actions.state.start_date ?? '';
    if (this.lastStart === next) return;
    this.lastStart = next;
    if (this.selected === 'today' || this.selected === 'training') this.paint();
  }
  showDocument() {
    this.nav.replaceChildren();
    this.heading.replaceChildren(el('h1', this.plan?.meta?.title ?? 'Plan'));
    if (this.plan?.meta?.subtitle) this.heading.append(el('p', this.plan.meta.subtitle));
    if (!this.plan) {
      this.content.replaceChildren(el('h2', text.empty), el('p', text.emptyDetail),
        el('code', 'python -m app.cli.plan_import /private/location/plan.json'));
      return;
    }
    this.showSections();
  }
  showSections() {
    const available = /** @type {Section[]} */ (sections(/** @type {import('./plan-types').Plan} */ (this.plan)));
    if (!this.selected || !available.includes(this.selected)) this.selected = available[0];
    for (const key of available) {
      const control = button(text[key], () => { this.selected = key; this.paint(); this.content.scrollTop = 0; });
      control.dataset.section = key;
      this.nav.append(control);
    }
    if (!available.length) this.content.replaceChildren(el('p', text.noSections));
    else this.paint();
  }
  async load() {
    if (this.loading || this.store.busy) return;
    this.loading = true;
    if (!this.version) this.content.replaceChildren(el('p', text.loading));
    this.root.setAttribute('aria-busy', 'true');
    const revision = this.revision;
    try {
      const response = await this.request('/api/plan');
      if (revision === this.revision) this.apply(/** @type {import('./plan-types').PlanView} */ (response));
    } catch {
      this.notice(text.failed, true, 'plan');
      if (!this.version) this.content.replaceChildren(el('p', text.failed), button(text.retry, () => this.load()));
    } finally { this.loading = false; this.root.setAttribute('aria-busy', 'false'); }
  }
  /** @param {import('./plan-types').PlanView} response */
  apply(response) {
    if (this.store.busy) return;
    const changed = response.version !== this.version || this.plan === null;
    this.plan = response.plan;
    this.version = response.version ?? 0;
    this.store.replace(response.state ?? { start_date: null, checked_item_ids: [] });
    this.status.textContent = '';
    if (changed) this.showDocument();
    else if (this.actions.today !== this.getToday()) this.paint();
  }
}
/** @typedef {{notice: import('./dashboard-data.js').Notify, request: typeof import('./api.js').request, getToday: () => string,
 * format: import('./plan-format.js').PlanFormat}} PlanOptions */
/** @param {PlanOptions} options */
export function setupPlan(options) {
  useFormat(options.format);
  return new PlanPage(options);
}

/** @type {PlanPage | undefined} */
let activePlan;
/** Keep the page's lifetime inside its lazy module. @param {PlanOptions} options */
export function loadPlan(options) {
  activePlan ??= setupPlan(options);
  return activePlan.load();
}
