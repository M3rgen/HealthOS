import { setupDialog } from './accessibility.js';
import { dataTypeLabel } from './data-types.js';
import { fullDateTime } from './dates.js';
import { $, closest, setAttr, setText } from './dom.js';
import { morphChildren } from './dom-morph.js';
import { relativeTime } from './relative-time.js';
import { escape, num } from './format.js';
import { t } from './i18n/index.js';
import { lower } from './locale.js';

/**
 * @typedef {{id: string, name: string, status: string, platform?: string | null, form_factor?: string | null,
 *   google_device_type?: string | null, manufacturer?: string | null, application?: string | null, recording_method?: string | null,
 *   last_data?: string | null, google_last_sync?: string | null, garmin_last_pull?: string | null, first_seen?: string | null,
 *   last_seen?: string | null, data_type_count: number, data_types: string[], previous_names: string[], fingerprints: string[],
 *   precedence?: {activity?: string, health?: string} | null}} Device
 * @typedef {{devices: Device[], thresholds: {connected_hours: number, delayed_days: number}, google?: string}} DevicesReport
 * @typedef {(value: string | null | undefined, fallback?: string) => string} When
 * @typedef {(value: string | null | undefined) => string | null} Stamp
 */
const text = t.devices;
const terms = text.terms;
// Status text travels with its color: the colour alone never carries the meaning.
/** @type {Record<string, [string, string]>} */
const STATUS = { bagli: [text.status.bagli, 'good'], gecikmeli: [text.status.gecikmeli, 'warning'], kopuk: [text.status.kopuk, 'bad'] };
/** @type {Record<string, string>} */
const PLATFORMS = text.platforms;
// Precedence picks which source is shown first; it never sums or hides the others.
/** @type {Record<string, string>} */
const RANKS = text.ranks;
/** @param {string | undefined} code */
const rankOf = code => RANKS[code ?? ''] ?? RANKS.unranked;
/** @param {Device} device */
const precedenceText = device => device.precedence ? text.precedence(rankOf(device.precedence.activity), rankOf(device.precedence.health)) : null;
/** @type {Record<string, string>} */
const FORMS = text.forms;
/** @type {Record<string, string>} */
const METHODS = text.methods;
/** @param {Record<string, string>} table @param {string | null | undefined} value */
const label = (table, value) => (value ? table[value] ?? value : null);

/** @param {Device} device */
const kind = device => [label(PLATFORMS, device.platform), label(FORMS, device.form_factor ?? device.google_device_type)].filter(Boolean).join(' · ');

/** @param {Device} device @param {When} when */
function deviceRow(device, when) {
  const [status, tone] = STATUS[device.status] ?? [text.unknownStatus, 'warning'];
  const google = device.google_last_sync ? `<span>${escape(terms.googleLastSync)}: ${when(device.google_last_sync)}</span>` : '';
  const pull = device.garmin_last_pull ? `<span>${escape(terms.lastPull)}: ${when(device.garmin_last_pull)}</span>` : '';
  const rank = precedenceText(device) ? `<span class="device-precedence">${escape(precedenceText(device))}</span>` : '';
  return `<li class="device-row" data-status="${escape(device.status)}" data-key="${escape(device.id)}">
    <div class="device-main"><span class="device-name">${escape(device.name)}</span>` +
      `${kind(device) ? `<span class="device-kind">${escape(kind(device))}</span>` : ''}<span class="dot ${escape(tone)}">${escape(status)}</span></div>
    <div class="device-meta"><span>${escape(terms.lastData)}: ${when(device.last_data, text.noDatedRecordInline)}</span>` +
      `<span>${escape(text.typeCount(num(device.data_type_count)))}</span>${google}${pull}${rank}</div>
    <button type="button" class="button outline device-details" data-device="${escape(device.id)}" aria-haspopup="dialog" ` +
      `aria-label="${escape(text.detailsLabel(device.name))}">${escape(text.details)}</button>
  </li>`;
}

/** @param {string} term @param {string | null | undefined} value */
function detail(term, value) {
  return value ? `<div><dt>${escape(term)}</dt><dd>${value}</dd></div>` : '';
}

/** @param {Device} device @param {string} status @param {Stamp} stamp */
function detailRows(device, status, stamp) {
  return [
    detail(terms.status, escape(status)),
    detail(terms.platform, escape(label(PLATFORMS, device.platform))),
    detail(terms.kind, escape(label(FORMS, device.form_factor ?? device.google_device_type))),
    detail(terms.manufacturer, escape(device.manufacturer)),
    detail(terms.application, escape(device.application)),
    detail(terms.recordingMethod, escape(label(METHODS, device.recording_method))),
    detail(terms.precedence, escape(precedenceText(device))),
    detail(terms.lastData, stamp(device.last_data) ?? escape(text.noDatedRecord)),
    detail(terms.googleLastSync, stamp(device.google_last_sync)),
    detail(terms.lastPull, stamp(device.garmin_last_pull)),
    detail(terms.firstSeen, stamp(device.first_seen)),
    detail(terms.lastSeen, stamp(device.last_seen)),
    detail(terms.dataTypes(num(device.data_type_count)), `<ul class="device-types">${device.data_types.map(type => `<li>${escape(dataTypeLabel(type))}</li>`).join('')}</ul>`),
    detail(terms.previousNames, device.previous_names.length ? escape(device.previous_names.join(', ')) : null),
    detail(terms.fingerprints, escape(device.fingerprints.join(', '))),
  ];
}

/** @param {Device[]} devices @param {When} when */
function devicesMarkup(devices, when) {
  return devices.map(device => deviceRow(device, when)).join('');
}

/** @param {Device} device @param {string} status @param {Stamp} stamp */
function deviceDetailsMarkup(device, status, stamp) {
  return detailRows(device, status, stamp).join('');
}

/** @param {Device[]} devices @param {DevicesReport['thresholds']} thresholds */
function paintSummary(devices, thresholds) {
  /** @param {string} status */
  const count = status => devices.filter(device => device.status === status).length;
  const parts = Object.entries(STATUS).map(([status, [word]]) => (count(status) ? `${count(status)} ${lower(word)}` : null)).filter(Boolean);
  setText($('devices-summary'), text.summary(devices.length, parts.join(' · ')));
  setText($('devices-thresholds'), text.thresholds(thresholds.connected_hours, thresholds.delayed_days));
}

/** @param {DevicesReport} report @param {When} when */
function paintList(report, when) {
  const list = $('devices-list');
  const { devices, thresholds } = report;
  $('devices-google-note').hidden = report.google !== 'unavailable';
  // An empty list is hidden rather than left as a zero-height focusable region.
  list.hidden = !devices.length;
  if (!devices.length) {
    setText($('devices-summary'), text.empty);
    setText($('devices-thresholds'), text.emptyNote);
    list.replaceChildren();
    return;
  }
  paintSummary(devices, thresholds);
  morphChildren(list, devicesMarkup(devices, when));
}

function paintFailed() {
  $('devices-list').replaceChildren();
  $('devices-list').hidden = true;
  $('devices-google-note').hidden = true;
  setText($('devices-thresholds'), '');
  setText($('devices-summary'), text.failed);
  $('devices-retry').hidden = false;
}

/** @param {() => string} getZone */
function createTimeFormat(getZone) {
  /** @param {string | null | undefined} value */
  const full = value => (value ? fullDateTime(value, getZone()) : null);
  /** @type {When} */
  const when = (value, fallback = text.none) => {
    const relative = relativeTime(value);
    return relative ? `<time datetime="${escape(value)}" title="${escape(full(value))}">${escape(relative)}</time>` : escape(fallback);
  };
  /** @type {Stamp} */
  const stamp = value => (value ? `${escape(full(value))}${relativeTime(value) ? ` · ${escape(relativeTime(value))}` : ''}` : null);
  return { when, stamp };
}

/** @param {{request: (url: string) => Promise<DevicesReport>, getZone: () => string}} parts */
export function setupDevices({ request, getZone }) {
  const dialog = $('devices-dialog');
  const modal = setupDialog(dialog);
  const { when, stamp } = createTimeFormat(getZone);
  /** @type {DevicesReport | null} */
  let report = null;
  let loading = false;

  /** @param {string} id @param {HTMLElement} trigger */
  function openDetails(id, trigger) {
    const device = report?.devices.find(item => item.id === id);
    if (!device) return;
    const [status] = STATUS[device.status] ?? [text.unknownStatus];
    $('devices-dialog-title').textContent = device.name;
    $('devices-details').innerHTML = deviceDetailsMarkup(device, status, stamp);
    modal.open({ trigger, initial: $('devices-close') });
  }

  // Only the first load, or one after a failure, says "loading": a repeat every minute stays silent.
  function showLoading() {
    setText($('devices-summary'), text.loading);
    $('devices-list').setAttribute('aria-busy', 'true');
  }

  async function refresh() {
    if (loading) return;
    loading = true;
    if (!report) showLoading();
    $('devices-retry').hidden = true;
    try {
      report = await request('/api/devices');
      paintList(report, when);
    } catch {
      report = null;
      paintFailed();
    } finally {
      loading = false;
      setAttr($('devices-list'), 'aria-busy', 'false');
    }
  }

  $('devices-list').addEventListener('click', event => {
    const button = closest(event, '[data-device]');
    if (button?.dataset.device) openDetails(button.dataset.device, button);
  });
  $('devices-close').addEventListener('click', () => dialog.close());
  $('devices-retry').addEventListener('click', refresh);
  return { refresh };
}
