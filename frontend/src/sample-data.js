// The synthetic sample data (`healthos demo`, /api/demo): offered where an empty installation has
// no source that can fill it from here, announced by a banner while the dashboard shows it, and
// removed in one click. Sample rows carry their own source code, DEMO (app/core/providers.py), so
// the dashboard itself says when it shows them. Sistem › Bağlantı lists the three ways to get data.
import { errorText, request } from './api.js';
import { $, setText } from './dom.js';
import { t } from './i18n/index.js';
import { sourceEnabled } from './sources.js';
import { state } from './state.js';

const text = t.sample;
export const SAMPLE_SOURCE = 'DEMO';
// Where a Garmin owner creates the Personal API key the server reads (docs/reference/garmin.md).
export const GARMIN_KEY_PAGE = 'https://developer.garmin.com/gc-developer-program/personal/personal-api';

/** The dashboard on screen holds sample values. @param {import('./state.js').DashboardData | null} data */
export const showsSample = data =>
  Boolean(data?.days.some(day => Object.values(day.sources ?? {}).some(source => source?.source === SAMPLE_SOURCE)));

/**
 * No source can fill the archive from here: Google is not connected and Garmin is not selected.
 * @param {import('./state.js').Health | null} health
 */
export const sampleOffered = health =>
  Boolean(health) && !sourceEnabled(health, 'garmin') && !(sourceEnabled(health, 'google') && health?.configuration === 'healthy');

let busy = false;
/**
 * POST loads the sample in the shown time zone, DELETE removes it; then the page fetches again.
 * A refusal or failure is said in `status`.
 * @param {'POST' | 'DELETE'} method @param {HTMLButtonElement} button @param {Element} status
 */
async function change(method, button, status) {
  if (busy) return;
  busy = true;
  button.disabled = true;
  button.setAttribute('aria-busy', 'true');
  setText(status, method === 'POST' ? text.loading : text.removing);
  try {
    const query = method === 'POST' ? `?${new URLSearchParams({ timezone: state.zone })}` : '';
    await request(`/api/demo${query}`, { method, headers: { 'X-HealthOS-Request': '1' } });
    setText(status, '');
    document.dispatchEvent(new Event('healthos:retry'));
  } catch (error) {
    setText(status, errorText(error));
  } finally {
    busy = false;
    button.disabled = false;
    button.removeAttribute('aria-busy');
  }
}

/** @param {HTMLButtonElement} button @param {Element} status */
const toggle = (button, status) => () => change(showsSample(state.data) ? 'DELETE' : 'POST', button, status);

/** One way to get data: its name, what to do, and an optional link or button. @param {string} name @param {string} how @param {HTMLElement} [action] */
function way(name, how, action) {
  const item = document.createElement('li');
  const title = document.createElement('strong');
  title.textContent = name;
  item.append(title, ` ${how} `);
  if (action) item.append(action);
  return item;
}

function buildGuide() {
  const guide = $('source-guide');
  const link = document.createElement('a');
  link.href = GARMIN_KEY_PAGE;
  link.target = '_blank';
  link.rel = 'noopener noreferrer';
  link.className = 'text-link';
  link.textContent = text.garminLink;
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'text-link';
  button.id = 'source-guide-sample';
  const status = document.createElement('span');
  status.setAttribute('role', 'status');
  button.addEventListener('click', toggle(button, status));
  const title = document.createElement('p');
  title.textContent = text.guideTitle;
  const list = document.createElement('ol');
  list.append(way('Garmin', text.garmin, link), way('Google', text.google), way(text.name, text.sample, button));
  list.lastElementChild?.append(status);
  guide.append(title, list);
  return button;
}

const banner = $('sample-banner');
const remove = $('sample-remove');
const offer = $('overview-state-sample');
const guideButton = buildGuide();
setText(remove, text.remove);
setText(offer, text.load);
remove.addEventListener('click', () => change('DELETE', remove, $('sample-banner-text')));
offer.addEventListener('click', toggle(offer, $('overview-state-detail')));

/**
 * Paints the banner, the empty overview's offer and the Sistem guide from the last dashboard and
 * health reports. `emptyOverview`: the overview shows its empty state (page-states.js).
 * @param {boolean} emptyOverview
 */
export function paintSample(emptyOverview) {
  const shown = showsSample(state.data);
  const offered = sampleOffered(state.health);
  banner.hidden = !shown;
  if (shown && !remove.hasAttribute('aria-busy')) setText($('sample-banner-text'), text.banner);
  offer.hidden = !(emptyOverview && offered && !shown);
  $('source-guide').hidden = !(offered || shown);
  setText(guideButton, shown ? text.remove : text.load);
}
