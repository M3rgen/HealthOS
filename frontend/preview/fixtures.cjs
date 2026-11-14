// SYNTHETIC preview fixtures. Every value here is invented; no account data.
// The clock is frozen at NOW so screenshots are deterministic.
const NOW = '2026-10-16T14:30:00Z';
const TODAY = NOW.slice(0, 10);

const iso = (date) => date.toISOString().slice(0, 10);
const civil = (value) => new Date(`${value}T12:00:00Z`);
const minus = (value, days) => { const d = civil(value); d.setUTCDate(d.getUTCDate() - days); return iso(d); };
// Small deterministic pseudo-random walk so charts look organic but stable.
const wave = (i, seed) => (Math.sin(i * 1.7 + seed) + Math.sin(i * 0.63 + seed * 2.1)) / 2;

const normalization = {
  status: 'unknown', last_attempt_at: null, last_success_at: null,
  last_error_at: null, last_error_code: null,
};
const garminStatus = {
  enabled: true, key_file_present: false, pull_interval_minutes: 120, lookback_days: 3,
  last_success_at: null, last_error: null, last_error_at: null, failing_kinds: [],
  raw_records: 0, responses: 0, normalization,
  kinds: { steps: { records: 0, newest_key: null, last_success_at: null,
    last_error: null, last_error_at: null, failing: false, backfill: null } },
};
const health = {
  status: 'healthy', database: 'healthy', scheduler: 'running', configuration: 'healthy',
  checked_at: '2026-10-16T14:28:00Z', last_sync_success: '2026-10-16T14:15:00Z',
  latest_health_write: '2026-10-16T14:15:00Z', last_backup_success: '2026-10-16T03:30:00Z', last_webhook_received: '2026-10-16T13:58:00Z',
  data_freshness: { heart_rate: '2026-10-16T14:05:00Z', steps: '2026-10-16T14:10:00Z' },
  pending_notifications: 0, failed_notifications: 0, failed_syncs: 0, stale_streams: [],
  reasons: [], active_sources: ['google'], service: 'healthos', ui_login_disabled: false,
  garmin: { last_success_at: null, last_error_at: null, last_error_code: null,
    lag_seconds: null, stale_after_seconds: 21600, normalization },
};

// Precedence (maintainer, 7 Nov 2026): Garmin first for sport activities, the Fitbit Air first for
// every other health metric. It only selects what is shown first; nothing is summed or dropped.
const PRECEDENCE = { activity: ['GARMIN', 'FITBIT', 'HEALTH_KIT'], health: ['FITBIT', 'GARMIN', 'HEALTH_KIT'] };
const SOURCE_LABELS = { FITBIT: 'Fitbit', GARMIN: 'Garmin', HEALTH_KIT: 'Apple Sağlık', MANUAL: 'Manuel' };
const METRICS = ['steps', 'sleep_seconds', 'resting_bpm', 'hrv_ms', 'spo2'];
// Default: every value comes from the Fitbit alone, so no cell carries a badge.
const sourcesOf = (row) => Object.fromEntries(METRICS.map((metric) => [metric,
  row[metric] == null ? { source: null, label: null, alternatives: [] } : { source: 'FITBIT', label: 'Fitbit', alternatives: [] }]));
// Garmin also measured steps and sleep on this day; the Fitbit stays first, Garmin is listed under it.
const withGarmin = (row) => ({ ...row, sources: { ...row.sources,
  steps: { source: 'FITBIT', label: 'Fitbit', alternatives: [{ source: 'GARMIN', label: 'Garmin', value: row.steps - 700 }] },
  sleep_seconds: { source: 'FITBIT', label: 'Fitbit', alternatives: [{ source: 'GARMIN', label: 'Garmin', value: row.sleep_seconds + 1500 }] } } });

// One seed per calendar date, so the dashboard, its past windows and the metric history all show
// the same value for the same day. It equals the day of the month in TODAY's month (16 Oct -> 16).
const seedOf = (date) => Number(TODAY.slice(-2)) - Math.round((civil(TODAY) - civil(date)) / 86400000);

function day(date, i = seedOf(date)) {
  const partial = date === TODAY;
  const missingSpo2 = i % 5 === 3;
  const row = {
    date,
    steps: Math.round((partial ? 6240 : 9100) + wave(i, 1) * 2600),
    sleep_seconds: Math.round(26400 + wave(i, 2) * 3600),
    resting_bpm: Math.round(58 + wave(i, 3) * 4),
    hrv_ms: 44 + Math.round(wave(i, 4) * 90) / 10,
    spo2: missingSpo2 ? null : 97 + Math.round(wave(i, 5) * 10) / 10,
    partial,
    status: partial ? 'partial' : missingSpo2 ? 'missing' : 'complete',
  };
  return { ...row, sources: sourcesOf(row) };
}

function dashboard(params) {
  const days = Math.max(1, Math.min(31, Number(params.get('days') || 7)));
  const end = params.get('end_date') || TODAY;
  const rows = Array.from({ length: days }, (_, i) => day(minus(end, i)));
  const daytime = (h) => h >= 7 && h <= 22;
  return {
    start_date: minus(end, days - 1), end_date: end, timezone: params.get('timezone') || 'UTC',
    profile: {
      height: { metric: 'height', value: 178, measured_at: '2026-05-29T08:00:00Z', source: 'Manuel' },
      weight: { metric: 'weight', value: 74.6, measured_at: '2026-09-30T07:30:00Z', source: 'Manuel' },
      body_fat: { metric: 'body_fat', value: 17.8, measured_at: '2026-09-30T07:30:00Z', source: 'Manuel' },
    },
    days: rows,
    precedence: PRECEDENCE,
    charts: {
      steps: Array.from({ length: 24 }, (_, h) => end === TODAY && h > 14 ? null
        : daytime(h) ? Math.round(420 + Math.max(0, wave(h, 7)) * 900 + (h === 8 || h === 18 ? 1100 : 0)) : (h === 6 ? 60 : 0)),
      heart_rate: Array.from({ length: 24 }, (_, h) => end === TODAY && h > 14 ? null
        : h === 3 ? null : Math.round((daytime(h) ? 72 : 57) + wave(h, 9) * 6 + (h === 18 ? 24 : 0))),
      sources: { heart_rate: { source: 'FITBIT', label: 'Fitbit', alternatives: [] }, steps: { source: 'FITBIT', label: 'Fitbit', alternatives: [] } },
    },
  };
}
// Scenario 'kaynaklar': two finished days also carry Garmin values for steps and sleep, and the
// hourly charts name the Garmin as a second source.
function dashboardWithSources(params) {
  const data = dashboard(params);
  return { ...data,
    days: data.days.map((row, i) => (i === 1 || i === 3 ? withGarmin(row) : row)),
    charts: { ...data.charts, sources: {
      heart_rate: { source: 'FITBIT', label: 'Fitbit', alternatives: [{ source: 'GARMIN', label: 'Garmin' }] },
      steps: { source: 'FITBIT', label: 'Fitbit', alternatives: [{ source: 'GARMIN', label: 'Garmin' }] } } } };
}

function history(params) {
  const metric = params.get('metric') || 'steps';
  const page = Number(params.get('page') || 1);
  const end = minus(TODAY, 30 * (page - 1));
  const days = Array.from({ length: 30 }, (_, i) => {
    const date = minus(end, 29 - i);
    const row = day(date);
    const value = metric === 'steps' ? row.steps : metric === 'sleep' ? row.sleep_seconds : row.resting_bpm;
    // A few days without a value, none of them in the dashboard's last 7 days (9 Oct, 28 Sep, 17 Sep...).
    const gap = Math.round((civil(TODAY) - civil(date)) / 86400000) % 11 === 7;
    return { date, value: gap ? null : value, partial: row.partial };
  });
  return {
    metric, page, total_pages: 7, start_date: minus(end, 29), end_date: end,
    earliest_date: minus(TODAY, 200), latest_date: TODAY, timezone: 'UTC', days,
  };
}

const measurements = [
  ['weight', 74.6, '2026-09-30T07:30:00Z'], ['body_fat', 17.8, '2026-09-30T07:30:00Z'],
  ['weight', 75.1, '2026-09-23T07:40:00Z'], ['body_fat', 18.2, '2026-09-23T07:40:00Z'],
  ['weight', 75.4, '2026-09-16T07:20:00Z'], ['muscle', 41.2, '2026-09-16T07:20:00Z'],
  ['weight', 75.9, '2026-09-09T07:35:00Z'], ['body_fat', 18.9, '2026-09-09T07:35:00Z'],
  ['weight', 76.3, '2026-09-02T07:30:00Z'], ['weight', 76.8, '2026-08-26T07:30:00Z'],
  ['height', 178, '2026-05-29T08:00:00Z'], ['weight', 77.4, '2026-08-19T07:30:00Z'],
  ['weight', 77.9, '2026-08-12T07:30:00Z'], ['body_fat', 19.6, '2026-08-12T07:30:00Z'],
// value: the API sends the stored decimal as text (app/services/dashboard_manual.py).
].map(([metric, value, measured_at], i) => ({ id: `m${i}`, metric, value: value.toFixed(4), measured_at, source: 'Manuel', flags: [] }));

const syncStatus = ['steps', 'heart-rate', 'daily-resting-heart-rate', 'sleep', 'heart-rate-variability',
  'oxygen-saturation', 'exercise', 'weight'].map((data_type, i) => ({
  data_type, status: i === 6 ? 'running' : 'completed', last_error: null,
  last_requested_from: '2026-09-24T00:00:00Z', last_requested_to: NOW,
  last_successful_sync: `2026-10-16T14:${String(15 - i).padStart(2, '0')}:00Z`,
  records_downloaded: [18422, 96211, 214, 189, 2340, 1580, 61, 37][i],
  last_run: null,
}));

const sports = ['Yürüyüş', 'Koşu', 'Bisiklet', 'Yüzme', 'Ağırlık antrenmanı', 'Yoga'];
// Every row names its source twice: `source` is the display label, `source_code` the stable code.
// `priority`/`group`/`alternatives` come from the activity precedence (Garmin first): a Fitbit
// session that overlaps a Garmin session is secondary and points at the Garmin row.
const sourceFields = (code) => ({ source: SOURCE_LABELS[code], source_code: code,
  provider: code === 'GARMIN' ? 'GARMIN' : code === 'MANUAL' ? 'MANUAL' : 'GOOGLE', priority: 'primary', group: null, alternatives: [] });
function workouts(params) {
  const month = (params.get('month') || `${TODAY.slice(0, 7)}-01`).slice(0, 7);
  const last = month === TODAY.slice(0, 7) ? Number(TODAY.slice(-2)) : 28;
  const rows = Array.from({ length: 12 }, (_, i) => {
    const date = `${month}-${String(Math.max(1, Math.round(1 + i * (last - 1) / 11))).padStart(2, '0')}`;
    const minutes = 25 + ((i * 17) % 50);
    const start = new Date(`${date}T${String(6 + (i % 3) * 3).padStart(2, '0')}:15:00Z`);
    const type = sports[i % sports.length];
    return {
      id: `w${i}`, exercise_type: type, start_time: start.toISOString(),
      end_time: new Date(start.getTime() + minutes * 60000).toISOString(), duration_seconds: minutes * 60,
      calories_kcal: 140 + ((i * 53) % 380), distance_meters: ['Yoga', 'Ağırlık antrenmanı'].includes(type) ? null : 1800 + i * 730,
      ...sourceFields(i % 4 === 1 ? 'MANUAL' : i === 7 ? 'GARMIN' : 'FITBIT'),
    };
  });
  // The watch recorded the run (w7); the Fitbit auto-detected a walk over the same minutes.
  const run = rows[7];
  const overlap = { id: 'w7-fitbit', exercise_type: 'Yürüyüş', start_time: new Date(Date.parse(run.start_time) + 5 * 60000).toISOString(),
    end_time: new Date(Date.parse(run.end_time) - 3 * 60000).toISOString(), duration_seconds: run.duration_seconds - 8 * 60,
    calories_kcal: run.calories_kcal - 60, distance_meters: run.distance_meters - 400,
    ...sourceFields('FITBIT'), priority: 'secondary', group: run.id };
  run.alternatives = [{ id: overlap.id, source: overlap.source, source_code: overlap.source_code, start_time: overlap.start_time, end_time: overlap.end_time }];
  rows.splice(8, 0, overlap);
  return rows.reverse();
}

// Umay's preview answer is written from the dashboard's own last 7 days, so the bubble and the
// table beside it never disagree. Umay speaks Turkish in every interface language.
const TR_DAYS = ['Paz', 'Pzt', 'Sal', 'Çar', 'Per', 'Cum', 'Cmt'];
const TR_MONTHS = ['Oca', 'Şub', 'Mar', 'Nis', 'May', 'Haz', 'Tem', 'Ağu', 'Eyl', 'Eki', 'Kas', 'Ara'];
const trNumber = (value) => String(value).replace(/\B(?=(\d{3})+(?!\d))/g, '.');
const trSleep = (seconds) => { const minutes = Math.round(seconds / 60); return `${Math.floor(minutes / 60)} sa ${minutes % 60} dk`; };
const trDay = (date) => { const d = civil(date); return `${TR_DAYS[d.getUTCDay()]} ${d.getUTCDate()} ${TR_MONTHS[d.getUTCMonth()]}`; };
const lastWeek = dashboard(new URLSearchParams()).days;
function weekAnswer(rows) {
  const today = rows.find((row) => row.partial);
  const done = rows.filter((row) => !row.partial);
  const mean = (key) => Math.round(done.reduce((sum, row) => sum + row[key], 0) / done.length);
  const bpm = done.map((row) => row.resting_bpm);
  const bySteps = [...done].sort((a, b) => b.steps - a.steps);
  const oldest = done.at(-1).date, newest = done[0].date;
  const span = `${civil(oldest).getUTCDate()}–${trDay(newest).split(' ').slice(1).join(' ')}`;
  return `**Merhaba!** Son ${done.length} tam gün (${span}): ortalama **${trNumber(mean('steps'))}** adım ve ` +
    `**${trSleep(mean('sleep_seconds'))}** uyku; dinlenme nabzı ${Math.min(...bpm)}–${Math.max(...bpm)} bpm.\n\n` +
    '| | Gün | Adım | Uyku |\n|---|---|---:|---:|\n' +
    [['En çok', bySteps[0]], ['En az', bySteps.at(-1)]].map(([label, row]) =>
      `| ${label} | ${trDay(row.date)} | ${trNumber(row.steps)} | ${trSleep(row.sleep_seconds)} |`).join('\n') +
    (today ? `\n\nBugün şimdiye kadar **${trNumber(today.steps)}** adım.` : '');
}
const chatAnswer = weekAnswer(lastWeek);
// The turn's transfer receipt (app/services/umay/health_transfer.py describe_result): one daily
// query for the same 7 days the answer reads.
const zeroCounts = { raw_records: 0, daily_summaries: 0, profile_measurements: 0, chart_points: 0,
  derived_summaries: 0, derived_observations: 0, metadata_items: 0 };
const weekRange = { first: lastWeek.at(-1).date, last: lastWeek[0].date };
const chatTransferCall = { action: 'daily', outcome: 'sent', counts: { ...zeroCounts, daily_summaries: lastWeek.length },
  unique_source_records: null, requested: weekRange, timezone: 'UTC', source_timezones: ['UTC'], data_dates: weekRange,
  profile_dates: null, metadata_dates: null,
  fields: ['days[].date', 'days[].steps', 'days[].sleep_seconds', 'days[].resting_bpm', 'days[].hrv_ms', 'days[].spo2', 'days[].status'] };
const chatTransfer = (status) => ({ type: 'health_transfer', turn: 'preview', status, calls: [chatTransferCall],
  counts: chatTransferCall.counts, failed_sends: 0 });

// GET /api/sync/coverage: every requested day retrieved with records (app/api/sync.py).
function coverage(params) {
  const start = new Date(`${params.get('start_date')}T00:00:00Z`), end = new Date(`${params.get('end_date')}T00:00:00Z`);
  const days = [];
  for (let at = start; at <= end; at = new Date(at.getTime() + 86400000))
    days.push({ date: at.toISOString().slice(0, 10), has_value: true, retrieval: 'completed_with_records', returned_records: 12 + days.length * 3, reason: null, closed: at.toISOString().slice(0, 10) < TODAY });
  const type = params.get('data_type') || 'steps';
  return { schema_version: '1', start_date: params.get('start_date'), end_date: params.get('end_date'), timezone: params.get('timezone') || 'UTC',
    note: 'Sentetik önizleme kapsamı.',
    supported_data_types: syncStatus.map(row => row.data_type), next_offset: null,
    summary: { days_without_value: 0, unknown_days: 0, incomplete_days: 0, not_attempted_days: 0, basis: 'type_days_in_page' },
    types: [{ data_type: type, presence_basis: 'metric_value',
      exclusions: { quarantined: 0, without_raw_record: 0, source_deleted: 0, superseded: 0, invalid_shape: 0 },
      source_regimes: [{ source: 'Fitbit', origin: 'google', device: 'Fitbit Air', form_factor: 'WATCH', platform: 'FITBIT', value_days: days.length, record_count: days.reduce((n, d) => n + d.returned_records, 0) }], days }] };
}

// GET /api/webhooks/status: notifications arriving, no failures.
const webhookStatus = { checked_at: '2026-10-16T14:28:00Z', receiver: { configured: true }, last_notification_at: '2026-10-16T13:58:00Z',
  silence: { state: 'receiving', warning: false, never_received: false, since: null, threshold_seconds: 604800 },
  counters: { scope: 'persistent_diagnostic_events', since: '2026-09-24T00:00:00Z', valid: 412, invalid: 0, expired: 0, reasons: {}, units: {} },
  subscription: { status: 'unknown', renewal_status: 'unknown', last_renewed_at: null, basis: 'no_subscription_client_or_evidence' },
  last_error: null };

// GET /api/devices: the archive's devices (app/services/devices.py). The watch was renamed by
// Google ("Google Fitbit Air" -> "Fitbit Air"): one row, the old name under previous_names.
// GitHub-only Apple extension: the System page's Apple Health import panel asks for its status.
// Receiver off and no import yet, as on an installation that has not used it.
const appleStatus = { receiver: { enabled: false, token_file_present: false, endpoint_configured: false, path: '/webhooks/apple-health' }, running: null, imports: [], sources: [] };

// Each device carries its provider, source code and precedence (activity / health rank). The Garmin
// watch is the pull-only second source: `last_seen` is its last successful pull.
const devices = {
  checked_at: NOW, thresholds: { connected_hours: 48, delayed_days: 7 }, google: 'ok', garmin: 'ok', apple: 'off', google_unmatched: [],
  precedence: PRECEDENCE,
  note: 'Son veri, kaynağın arşivdeki en yeni kaydının fiziksel zamanı; eşitleme zamanı değil.',
  devices: [
    { id: '0a1b2c3d4e5f', name: 'Fitbit Air', platform: 'FITBIT', provider: 'GOOGLE', source_code: 'FITBIT', form_factor: 'WATCH', manufacturer: null, application: null,
      recording_method: 'DERIVED', previous_names: ['Google Fitbit Air'], fingerprints: ['0a1b2c3d4e5f', '9f8e7d6c5b4a'],
      data_types: ['daily-resting-heart-rate', 'heart-rate', 'oxygen-saturation', 'sleep', 'steps'], data_type_count: 5,
      first_seen: '2025-11-02T06:10:00Z', last_seen: '2026-10-16T14:15:00Z', last_data: '2026-10-16T14:05:00Z', status: 'bagli',
      google_last_sync: '2026-10-16T14:12:00Z', google_device_type: 'WATCH', precedence: { activity: 'secondary', health: 'primary' } },
    { id: '1b2c3d4e5f60', name: 'iPhone', platform: 'HEALTH_KIT', provider: 'GOOGLE', source_code: 'HEALTH_KIT', form_factor: 'PHONE', manufacturer: 'Example Inc.',
      application: 'com.example.health', recording_method: 'PASSIVELY_MEASURED', previous_names: [], fingerprints: ['1b2c3d4e5f60'],
      data_types: ['steps'], data_type_count: 1, first_seen: '2026-03-20T08:00:00Z', last_seen: '2026-10-14T09:00:00Z',
      last_data: '2026-10-13T18:40:00Z', status: 'gecikmeli', precedence: { activity: 'secondary', health: 'secondary' } },
    { id: '2c3d4e5f6071', name: 'Aria Example', platform: 'FITBIT', provider: 'GOOGLE', source_code: 'FITBIT', form_factor: 'SCALE', manufacturer: null, application: null,
      recording_method: 'MANUALLY_ENTERED', previous_names: [], fingerprints: ['2c3d4e5f6071'], data_types: ['weight'], data_type_count: 1,
      first_seen: '2026-05-01T07:00:00Z', last_seen: '2026-09-06T07:30:00Z', last_data: '2026-09-06T07:30:00Z', status: 'kopuk',
      precedence: { activity: 'unranked', health: 'unranked' } },
    { id: 'garmin', name: 'Garmin', platform: 'GARMIN', provider: 'GARMIN', source_code: 'GARMIN', form_factor: 'WATCH', manufacturer: 'Garmin', application: null,
      recording_method: null, previous_names: [], fingerprints: [], data_types: ['activities', 'daily-summaries', 'heart-rate', 'sleep', 'steps'], data_type_count: 5,
      first_seen: '2026-09-20T06:00:00Z', last_seen: '2026-10-16T14:00:00Z', last_data: '2026-10-16T13:40:00Z', status: 'bagli',
      garmin_last_pull: '2026-10-16T14:00:00Z', precedence: { activity: 'primary', health: 'secondary' } },
  ],
};

// Alternate data sets for edge-case views. 'sync-error' shows the notice
// banner; 'empty' is a fresh install: Google not set up and no records yet.
function scenario(name) {
  // Two sources disagree on steps and sleep for two days: badges, toggles and the sub-row appear.
  if (name === 'kaynaklar') return { dashboard: dashboardWithSources };
  if (name === 'sync-error') return {
    health: { ...health, failed_syncs: 1 },
    syncStatus: syncStatus.map((row, i) => i === 3 ? { ...row, status: 'failed', last_error: 'Google API zaman aşımı (synthetic).' } : row),
  };
  if (name === 'empty') {
    const blank = (params) => {
      const data = dashboard(params);
      return { ...data, profile: {}, charts: { steps: data.charts.steps.map(() => null), heart_rate: data.charts.heart_rate.map(() => null) },
        days: data.days.map((d) => ({ ...d, steps: null, sleep_seconds: null, resting_bpm: null, hrv_ms: null, spo2: null, status: d.partial ? 'partial' : 'empty' })) };
    };
    return {
      health: { ...health, configuration: 'degraded', last_sync_success: null, latest_health_write: null, last_webhook_received: null, data_freshness: {} },
      dashboard: blank, measurements: [], syncStatus: [], workouts: () => [],
      coverage: (params) => ({ ...coverage(params), supported_data_types: [], types: [], summary: { days_without_value: 0, unknown_days: 7, incomplete_days: 0 } }),
      devices: { ...devices, google: 'unavailable', devices: [] },
      webhookStatus: { ...webhookStatus, receiver: { configured: false }, last_notification_at: null, silence: { state: 'unconfigured', warning: false, never_received: true, since: null, threshold_seconds: 604800 },
        counters: { ...webhookStatus.counters, since: null, valid: 0 } },
      history: (params) => ({ ...history(params), total_pages: 1, earliest_date: null, days: history(params).days.map((d) => ({ ...d, value: null })) }),
    };
  }
  // A fresh install with no source selected (HEALTHOS_SOURCES=none): the sample data is offered.
  if (name === 'none') {
    const empty = scenario('empty');
    return { ...empty, health: { ...empty.health, active_sources: [], configuration: 'healthy' } };
  }
  // The same install after `healthos demo`: every value carries the sample's own source code.
  if (name === 'sample') {
    const demo = { source: 'DEMO', label: 'Örnek veri', alternatives: [] };
    const sampled = (params) => {
      const data = dashboard(params);
      return { ...data, profile: { weight: { ...data.profile.weight, source: 'Örnek veri' } },
        days: data.days.map((row) => ({ ...row, sources: Object.fromEntries(METRICS.map((metric) => [metric, row[metric] == null ? sourcesOf(row)[metric] : demo])) })),
        charts: { ...data.charts, sources: { heart_rate: demo, steps: demo } } };
    };
    return { health: { ...health, active_sources: [], last_sync_success: null, latest_health_write: null, data_freshness: {} },
      dashboard: sampled, syncStatus: [], devices: { ...devices, google: 'unavailable', devices: [] },
      workouts: (params) => workouts(params).filter((row) => row.source_code !== 'MANUAL')
        .map((row) => ({ ...row, source: 'Örnek veri', source_code: 'DEMO', provider: 'DEMO', alternatives: [] })) };
  }
  return {};
}

// GET /api/settings/appearance on a server with nothing stored yet (app/api/settings.py).
const appearanceSettings = { selection: {}, updated_at: null };

// Synthetic upload files for the appearance views/tests (generated, no assets).
const zlib = require('node:zlib');
function crc32(buffer) {
  let crc = ~0;
  for (const byte of buffer) { crc ^= byte; for (let k = 0; k < 8; k++) crc = (crc >>> 1) ^ (0xEDB88320 & -(crc & 1)); }
  return ~crc >>> 0;
}
function syntheticPng(width, height, paint) {
  const chunk = (type, data) => {
    const body = Buffer.concat([Buffer.from(type), data]);
    const out = Buffer.alloc(12 + data.length);
    out.writeUInt32BE(data.length, 0); body.copy(out, 4); out.writeUInt32BE(crc32(body), 8 + data.length);
    return out;
  };
  const rows = Buffer.alloc((width * 3 + 1) * height);
  for (let y = 0; y < height; y++) for (let x = 0; x < width; x++) {
    const [r, g, b] = paint(x / width, y / height);
    rows.set([r, g, b], y * (width * 3 + 1) + 1 + x * 3);
  }
  const header = Buffer.alloc(13);
  header.writeUInt32BE(width, 0); header.writeUInt32BE(height, 4); header.set([8, 2, 0, 0, 0], 8);
  return Buffer.concat([Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]), chunk('IHDR', header), chunk('IDAT', zlib.deflateSync(rows)), chunk('IEND', Buffer.alloc(0))]);
}
// A teal disc on a dusk gradient: reads as a portrait-like avatar when cropped.
const uploadAvatarPng = () => syntheticPng(320, 240, (u, v) => {
  const d = Math.hypot(u - .5, (v - .45) * .75);
  return d < .2 ? [72, 216, 176] : [Math.round(40 + 60 * v), Math.round(30 + 30 * u), Math.round(70 + 80 * (1 - v))];
});
const uploadBackgroundPng = () => syntheticPng(640, 400, (u, v) => [Math.round(30 + 90 * u), Math.round(50 + 40 * v), Math.round(90 + 60 * (1 - u))]);
// Includes a script and an id marker: tests assert neither ever reaches the DOM.
const uploadLogoSvg = () => Buffer.from('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48" id="uploaded-svg-marker"><script>window.uploadedSvgRan=true</script><circle cx="24" cy="24" r="18" fill="none" stroke="#48d88c" stroke-width="5"/><path d="M15 24h18" stroke="#48d88c" stroke-width="5" stroke-linecap="round"/></svg>');

module.exports = { NOW, TODAY, health, garminStatus, dashboard, dashboardWithSources, history, measurements, syncStatus, workouts, coverage, webhookStatus, devices, appleStatus, chatAnswer, chatTransfer, scenario, appearanceSettings, syntheticPng, uploadAvatarPng, uploadBackgroundPng, uploadLogoSvg };
