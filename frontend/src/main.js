// The dashboard's composition root: builds each part, wires their callbacks and starts polling.
// Everything else lives in its own module; nothing here renders.
import './style.css';
import { focusVisible } from './accessibility.js';
import { fetchHealth, request } from './api.js';
import { setupAppearance } from './appearance.js';
import { setupBodyLoader } from './body-loader.js';
import { setupCalendar } from './calendar.js';
import { setupChat } from './chat.js';
import { paintHealth, paintHealthFailed } from './connection-panel.js';
import { createDashboardData } from './dashboard-data.js';
import { civilDate, civilParts, clockTime, fullDate, shiftDay, todayKey } from './dates.js';
import { setupDisplaySettings } from './display-settings.js';
import { $ } from './dom.js';
import { duration, escape, num } from './format.js';
import { setupHistoryPanel } from './history-panel.js';
import { paintHistory, paintHistoryFailed, setupHistoryRows } from './history-table.js';
import { languageFallback, t } from './i18n/index.js';
import { number } from './locale.js';
import { iconMarkup, renderIcons } from './icons.js';
import { setupLanguageSelect } from './language-select.js';
import { setupMeasurementFlags } from './measurement-flags.js';
import { setupMeasurementList } from './measurement-list.js';
import { setupMeasurements } from './measurements.js';
import { setupMetricHistory } from './metric-history.js';
import { createNotice } from './notice.js';
import { paintCards, paintCardsFailed } from './overview-cards.js';
import { setupOverviewTabs } from './overview-tabs.js';
import { setupPages } from './pages.js';
import { setPageStatus } from './page-status.js';
import { createPolling } from './polling.js';
import { setupProfileValues } from './profile-values.js';
import { state } from './state.js';
import { setupScrollFocus } from './scroll-focus.js';
import { setupSyncAction } from './sync-action.js';
import { setupSystemPage } from './system-page.js';

renderIcons();
setupScrollFocus();
setupAppearance();
setupOverviewTabs(/** @type {HTMLElement} */ (document.querySelector('.overview-grid')));
setupLanguageSelect();

const notices = createNotice({
  box: $('notice'),
  text: $('notice-text'),
  close: $('notice-close'),
  // Built here so a retry control needs no markup of its own; it sits before the close button.
  action: Object.assign(document.createElement('button'), { type: 'button', className: 'button outline notice-action', hidden: true }),
  onClosed: () => focusVisible(document.querySelector('[data-page][aria-current="page"]')),
});
// `source` names the request that raised the notice; its next success clears an error from it.
/** @type {(message: string, error?: boolean, source?: string, detail?: string, action?: import('./notice.js').NoticeAction | null) => void} */
const notice = (message, error = false, source = 'general', detail = '', action = null) =>
  notices.show(message, { error, source, detail, action });
const clearNotice = (/** @type {string[]} */ ...sources) => notices.clear(...sources);

const getZone = () => state.zone;
const getToday = () => todayKey(state.zone);

const settings = setupDisplaySettings({
  onZoneChanged: async () => {
    await refreshPage();
  },
  onMotionChanged: () => body.updateMotion(),
});
const body = setupBodyLoader($('body-model'), { getMotion: () => state.motion });
const profileValues = setupProfileValues({ iconMarkup, getZone });
setupChat(getZone);
setupMetricHistory(getZone);
setupHistoryRows();
const calendar = setupCalendar({
  getToday,
  getSelected: () => state.selectedDate,
  onSelect: date => {
    state.selectedDate = date === getToday() ? null : date;
    dashboard.refresh();
  },
});
const historyPanel = setupHistoryPanel({ request, getZone, getToday, paint: paintHistory });
const measurements = setupMeasurementList({ request, notice, clearNotice });

// Until the first answer (data or failure) the overview's figures pulse, so a slow load does not
// look like an archive of dashes (styles/overview-metrics.css).
$('overview').setAttribute('aria-busy', 'true');
const firstAnswer = () => $('overview').setAttribute('aria-busy', 'false');
const dashboard = createDashboardData({
  request,
  fetchHealth,
  query() {
    const params = new URLSearchParams({ days: '7', timezone: state.zone });
    if (state.selectedDate) params.set('end_date', state.selectedDate);
    return params;
  },
  paint: {
    data(data) {
      paintCards(data, { profileValues, body });
      // Only today's dashboard can describe the archive; a selected week/month cannot.
      // Publish here even while the monthly table is open and paints a different response.
      if (data.end_date === getToday()) {
        setPageStatus('overview', {
          rows: data.days.length,
          hasValues: data.days.some(row =>
            [row.steps, row.sleep_seconds, row.resting_bpm, row.hrv_ms, row.spo2].some(value => value != null)),
        });
      }
      historyPanel.update(data);
      firstAnswer();
    },
    dataFailed() {
      paintCardsFailed();
      paintHistoryFailed();
      firstAnswer();
    },
    health: paintHealth,
    healthFailed: paintHealthFailed,
  },
  notice,
  clearNotice,
  onHealthFirstKnown() {
    if (state.activePage === 'system') system.load();
  },
  afterRefresh: () => calendar.update(),
  retry: () => refreshPage(),
});
const system = setupSystemPage({ request, notice, clearNotice, refresh: dashboard.refresh });
const planFormat = { civilDate, civilParts, shiftDay, number };
const loadPlan = () => import('./plan.js')
  .then(module => module.loadPlan({ notice, request, getToday, format: planFormat }))
  .catch(pageOutdated);
// The Sports page, like Plan, loads with its first visit (workouts.js keeps its one controller).
const workoutOptions = {
  request, getZone, getToday, escape, num, duration,
  fullDate: (/** @type {string} */ value) => fullDate(value, state.zone),
  timeOf: (/** @type {string} */ value) => clockTime(value, state.zone),
};
const loadWorkouts = () => import('./workouts.js').then(module => module.loadWorkouts(workoutOptions)).catch(pageOutdated);
async function refreshPage() {
  await dashboard.refresh();
  if (state.activePage === 'system') await system.load();
  if (state.activePage === 'sports') await loadWorkouts();
  if (state.activePage === 'plan') await loadPlan();
}

const pageLoaders = { plan: loadPlan, measurements: measurements.load, system: system.load, sports: loadWorkouts };
setupPages({
  onChange: () => body.updateMotion(),
  loaders: pageLoaders,
  // The system page waits for /health on a load: onHealthFirstKnown, or the check after the first refresh.
  firstLoaders: { plan: loadPlan, measurements: measurements.load, sports: loadWorkouts },
});
setupMeasurementFlags({ request, notice, reload: () => dashboard.refresh().then(measurements.load) });
setupMeasurements({
  request, notice,
  getProfile: () => state.data?.profile,
  onSaved: async () => {
    await dashboard.refresh();
    if (state.activePage === 'measurements') await measurements.load();
  },
});
setupSyncAction({ request, notice, clearNotice, refresh: dashboard.refresh });
$('refresh-system').addEventListener('click', refreshPage);
// "Yeniden dene" of an empty or failed page (page-states.js): the same fetch as the notice's retry.
document.addEventListener('healthos:retry', () => {
  refreshPage();
  if (state.activePage === 'measurements') measurements.load();
});

// Polling starts before the first load: a failed first load still has its retries.
const polling = createPolling(
  async () => {
    await settings.applyRemote(await settings.sync());
    await refreshPage();
  },
  { paused: () => document.hidden || $('measurement-dialog').open },
);
polling.start();
// Back on a tab that was hidden past a tick: the cards and today's date catch up at once.
document.addEventListener('visibilitychange', () => { if (!document.hidden) polling.catchUp(); });
window.addEventListener('pagehide', () => polling.stop());
// Back/forward cache: the page returns frozen as it was left, so polling restarts and the data,
// possibly hours old, is fetched again at once.
window.addEventListener('pageshow', event => {
  if (!event.persisted) return;
  polling.start();
  refreshPage();
});

// Every part of the page is wired: from here a failed request or chunk is reported on the page,
// so boot-guard.js stops watching (a failure before this point shows its full-screen message).
document.documentElement.dataset.booted = 'true';

const reloadPage = { label: t.common.reload, run: () => location.reload() };
function pageOutdated() { notice(t.common.pageOutdated, true, 'chunk', '', reloadPage); }
if (languageFallback) notice(t.common.languageUnavailable, true, 'language', '', reloadPage);
// A chunk that cannot be fetched after the start, typically a tab left open across an update.
window.addEventListener('vite:preloadError', pageOutdated);

// Asked in parallel with the first refresh, so the first paint never waits for it.
const firstDisplaySync = settings.sync();
await dashboard.refresh();
// Opened at #/system with the service down: onHealthFirstKnown never fired.
if (state.activePage === 'system' && !state.health) system.load();
await settings.applyRemote(await firstDisplaySync);
