import { setupDialog, focusVisible } from './accessibility.js';
import { ApiError, errorText } from './api.js';
import { dataTypeLabel } from './data-types.js';
import { civilFull, civilRange, shiftDay } from './dates.js';
import { $, setAttr, setText } from './dom.js';
import { morphChildren } from './dom-morph.js';
import { escape, num } from './format.js';
import { t } from './i18n/index.js';

/**
 * @typedef {{source: string, value_days: number, record_count: number}} CoverageSource
 * @typedef {{date: string, has_value?: boolean | null, retrieval: string, returned_records?: number | null, reason?: string | null}} CoverageDay
 * @typedef {{data_type: string, presence_basis: string, source_regimes: CoverageSource[], exclusions?: Record<string, number> | null, days: CoverageDay[]}} CoverageType
 * @typedef {{days_without_value: number, unknown_days: number, incomplete_days: number, not_attempted_days: number}} CoverageTotals
 * @typedef {{start: string, end: string, type: string}} Selection
 */
const text = t.coverage;
/** @type {Record<string, string>} */
const labels = text.retrieval;
/** @type {Record<string, string>} */
const reasons = text.reasons;
/** @type {Record<string, string>} */
const exclusionLabels = text.exclusions;
/** @param {CoverageDay} day */
const valueText = day => (day.has_value == null ? text.value.unknown : day.has_value ? text.value.present : text.value.absent);

/** @param {CoverageType} type */
function typeMarkup(type) {
  const sources = type.source_regimes.map(source =>
    `<li>${escape(text.source(source.source, num(source.value_days), num(source.record_count)))}</li>`
  ).join('');
  const exclusions = type.exclusions
    ? Object.entries(type.exclusions).filter(([, value]) => value > 0)
      .map(([key, value]) => `${escape(exclusionLabels[key] ?? key)}: ${num(value)}`).join(' · ')
    : '';
  return `<section class="coverage-type" data-key="${escape(type.data_type)}"><h3>${escape(dataTypeLabel(type.data_type))}</h3>
      <p class="fine-print">${escape(type.presence_basis === 'eligible_record'
        ? text.presence.eligibleRecord : text.presence.rules)}</p>
      ${sources ? `<ul class="coverage-sources" aria-label="${escape(text.sourcesLabel)}">${sources}</ul>` : ''}
      ${exclusions ? `<p class="fine-print">${escape(text.excluded)} · ${exclusions}</p>` : ''}
      <ul class="coverage-days">${type.days.map(day => `<li data-key="${escape(day.date)}">
        <time datetime="${escape(day.date)}">${escape(civilFull(day.date))}</time>
        <span>${escape(valueText(day))}</span>
        <span>${escape(labels[day.retrieval] ?? labels.unknown)}${day.returned_records != null
          ? ` · ${escape(text.returned(num(day.returned_records)))}` : ''}</span>
        ${day.reason ? `<small>${escape(reasons[day.reason] ?? day.reason)}</small>` : ''}
      </li>`).join('')}</ul></section>`;
}

/** @param {CoverageType[]} types */
function coverageMarkup(types) {
  return types.map(typeMarkup).join('');
}

/** Rebuilds the type picker only when the list of types changed: an open picker survives a refresh. @param {string[]} knownTypes */
function paintTypeOptions(knownTypes) {
  const select = $('coverage-type');
  const shown = [...select.options].slice(1).map(option => option.value);
  if (shown.join('\n') === knownTypes.join('\n')) return;
  const selection = select.value;
  select.replaceChildren(new Option(text.allTypes, ''), ...knownTypes.map(name => new Option(dataTypeLabel(name), name)));
  select.value = selection;
}

/** @param {{all: CoverageType[], totals: CoverageTotals, knownTypes: string[]}} report @param {string} type @param {string} period */
function paintReport({ all, totals, knownTypes }, type, period) {
  paintTypeOptions(knownTypes);
  // The report covers only the selected type (steps by default); say which.
  const summary = text.summary(type ? dataTypeLabel(type) : text.allTypes, num(totals.days_without_value), num(totals.unknown_days),
    num(totals.incomplete_days), num(totals.not_attempted_days));
  setText($('coverage-summary'), summary);
  setText($('coverage-status'), `${period} · ${summary}`);
  morphChildren($('coverage-results'), coverageMarkup(all));
}

/**
 * Follows next_offset to the end; null when a newer refresh took over meanwhile.
 * @param {typeof import('./api.js').request} request @param {URLSearchParams} query
 * @param {() => boolean} isCurrent @param {(received: number) => void} onPage
 */
async function fetchAllPages(request, query, isCurrent, onPage) {
  /** @type {CoverageType[]} */
  const all = [];
  /** @type {CoverageTotals} */
  const totals = { days_without_value: 0, unknown_days: 0, incomplete_days: 0, not_attempted_days: 0 };
  /** @type {string[]} */
  let knownTypes;
  /** @type {number | null} */
  let offset = 0;
  do {
    query.set('offset', String(offset));
    /** @type {import('./api-types').CoveragePage} */
    const result = await request(`/api/sync/coverage?${query}`);
    if (!isCurrent()) return null;
    knownTypes = result.supported_data_types;
    all.push(...result.types);
    for (const key of /** @type {(keyof CoverageTotals)[]} */ (Object.keys(totals))) totals[key] += result.summary[key] ?? 0;
    offset = result.next_offset;
    onPage(all.length);
  } while (offset != null);
  return { all, totals, knownTypes };
}

function showLoading() {
  setText($('coverage-summary'), text.loading);
  setText($('coverage-status'), text.loading);
  $('coverage-results').replaceChildren();
  $('coverage-results').setAttribute('aria-busy', 'true');
}

/** @param {unknown} error */
function paintFailed(error) {
  setText($('coverage-summary'), text.failed);
  // Shortening the period helps only a report that was too large or too slow to build.
  const advice = error instanceof ApiError && (error.kind === 'timeout' || error.kind === 'server' || error.status === 413);
  setText($('coverage-status'), `${text.failedDetail(errorText(error))}${advice ? ` ${text.shortenAdvice}` : ''}`);
  $('coverage-retry').hidden = false;
}

/** The range and type the dialog's fields hold now. @returns {Selection} */
const readFields = () => ({ start: $('coverage-start').value, end: $('coverage-end').value, type: $('coverage-type').value });

/** A submitted selection, ready to ask for. @param {Selection} selection @param {() => string} getZone */
function selectionQuery({ start, end, type }, getZone) {
  const zone = getZone();
  const query = new URLSearchParams({ start_date: start, end_date: end, timezone: zone, limit: '4' });
  if (type) query.set('data_type', type);
  return { query, key: query.toString(), type, period: `${start && end ? civilRange(start, end) : ''} · ${zone}` };
}

/** @param {{request: typeof import('./api.js').request, getZone: () => string, getToday: () => string}} parts */
export function setupCoverage({ request, getZone, getToday }) {
  const dialog = $('coverage-dialog');
  const modal = setupDialog(dialog);
  let sequence = 0;
  let initialized = false;
  /** @type {string | null} */
  let loadingKey = null;
  // The selection the results on screen answer. Asking again (every minute) updates them in place.
  /** @type {string | null} */
  let paintedFor = null;

  // The selection last asked for with "Göster". The minute poll repeats it; a field being edited
  // (an empty date, a type not yet confirmed) is the user's until they submit it.
  /** @type {Selection | null} */
  let submitted = null;

  function initialize() {
    if (initialized) return;
    initialized = true;
    $('coverage-end').value = shiftDay(getToday(), -1);
    $('coverage-start').value = shiftDay($('coverage-end').value, -6);
    submitted = readFields();
  }

  async function refresh() {
    initialize();
    const { query, key, type, period } = selectionQuery(/** @type {Selection} */ (submitted), getZone);
    if (loadingKey === key) return;
    loadingKey = key;
    const current = ++sequence;
    const repeat = paintedFor === key;
    setText($('coverage-period'), period);
    if (!repeat) showLoading();
    if (document.activeElement === $('coverage-retry')) focusVisible($('coverage-form').querySelector('button'));
    $('coverage-retry').hidden = true;
    try {
      const onPage = repeat ? () => {} : (/** @type {number} */ received) => setText($('coverage-status'), text.typesReceived(received));
      const report = await fetchAllPages(request, query, () => current === sequence, onPage);
      if (!report) return;
      paintReport(report, type, period);
      paintedFor = key;
    } catch (error) {
      if (current !== sequence) return;
      paintedFor = null;
      paintFailed(error);
    } finally {
      if (current === sequence) {
        loadingKey = null;
        setAttr($('coverage-results'), 'aria-busy', 'false');
      }
    }
  }
  // index.html's only pre-filled option is the API code; the dialog shows its readable name.
  $('coverage-type').options[0].textContent = dataTypeLabel('steps');
  $('coverage-open').addEventListener('click', () => modal.open({ initial: $('coverage-start') }));
  $('coverage-close').addEventListener('click', () => dialog.close());
  $('coverage-retry').addEventListener('click', refresh);
  $('coverage-form').addEventListener('submit', event => {
    event.preventDefault();
    // What the user typed wins over the default week, even before the page's first load.
    initialized = true;
    submitted = readFields();
    refresh();
  });
  return { refresh };
}
