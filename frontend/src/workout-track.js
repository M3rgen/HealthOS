// Pure helpers of the workout detail view: units, the chart axes and series, pauses, laps, and
// the sample index a chart position or a route point stands for. The track API sends SI units in
// equal-length columns (docs/reference/garmin-tracks.md); every chart and the map share one index.

/**
 * @typedef {(number | null)[]} Column
 * @typedef {{time: number[], latitude?: Column, longitude?: Column, altitude?: Column, speed?: Column, heart_rate?: Column,
 *   distance?: Column, cadence?: Column, power?: Column, temperature?: Column}} TrackSamples
 * @typedef {{start_time?: Column, time?: Column, elapsed_seconds?: Column, moving_seconds?: Column, distance?: Column,
 *   average_speed?: Column, maximum_speed?: Column, average_heart_rate?: Column, maximum_heart_rate?: Column,
 *   start_index?: Column, end_index?: Column}} TrackLaps
 * @typedef {{start_time: string | null, elapsed_seconds: number, moving_seconds: number, distance: number,
 *   average_speed: number | null, maximum_speed: number | null, ascent: number | null, descent: number | null,
 *   bounds: number[] | null}} TrackSummary
 * @typedef {{workout_id: string, exercise_type: string | null, summary: TrackSummary, point_count: number,
 *   source_point_count: number, samples: TrackSamples, laps: TrackLaps, events: {time: number[], action: string[]}}} Track
 * @typedef {'time' | 'distance'} Axis
 * @typedef {'speed' | 'pace' | 'heart_rate' | 'altitude' | 'cadence' | 'power' | 'temperature'} SeriesKey
 * @typedef {{key: SeriesKey, values: Column, inverted: boolean}} Series
 */

// Sports whose speed reads better as a pace (min/km). Anything else shows km/h first.
const PACE_SPORTS = new Set(['WALKING', 'RUNNING', 'RUN_WALK', 'TREADMILL_RUNNING', 'HIKING']);
// Slower than this is standing still: a pace of an hour per kilometre would flatten the chart.
export const SLOWEST_PACE = 30 * 60;
const CHANNELS = /** @type {const} */ (['heart_rate', 'altitude', 'cadence', 'power', 'temperature']);

/** @param {string | null | undefined} type */
export const pacedSport = type => PACE_SPORTS.has(String(type ?? ''));

/** km/h from m/s. @param {number | null | undefined} speed */
export const kmh = speed => (speed == null ? null : speed * 3.6);

/** Seconds per kilometre from m/s; none at rest or slower than SLOWEST_PACE. @param {number | null | undefined} speed */
export function pace(speed) {
  if (speed == null || !(speed > 0)) return null;
  const seconds = 1000 / speed;
  return seconds > SLOWEST_PACE ? null : seconds;
}

/** "5:07" for a pace or a short time, "1:02:05" past an hour. @param {number | null | undefined} seconds */
export function clock(seconds) {
  if (seconds == null || !Number.isFinite(seconds)) return '—';
  const whole = Math.round(Math.max(0, seconds));
  const hours = Math.floor(whole / 3600), minutes = Math.floor((whole % 3600) / 60), rest = whole % 60;
  const pad = (/** @type {number} */ value) => String(value).padStart(2, '0');
  return hours ? `${hours}:${pad(minutes)}:${pad(rest)}` : `${minutes}:${pad(rest)}`;
}

/** @param {Column | undefined} column */
const present = column => (column ?? []).filter(value => value != null).length;

/** The horizontal axes a track can be drawn on: distance needs an odometer that moves. @param {TrackSamples} samples @returns {Axis[]} */
export function axes(samples) {
  const odometer = (samples.distance ?? []).filter(value => value != null);
  return odometer.length >= 2 && odometer[odometer.length - 1] > odometer[0] ? ['time', 'distance'] : ['time'];
}

/**
 * The x of every sample on an axis, without gaps: a missing odometer reading takes the one before
 * it (the first ones the first known), so the values never decrease and can be searched.
 * @param {TrackSamples} samples @param {Axis} axis @returns {number[]}
 */
export function positions(samples, axis) {
  if (axis === 'time' || !samples.distance) return samples.time;
  const column = samples.distance;
  let last = column.find(value => value != null) ?? 0;
  return column.map(value => {
    if (value != null && value >= last) last = value;
    return last;
  });
}

/** Index of the sample nearest `x` in non-decreasing `xs`; the earlier one on a tie. @param {number[]} xs @param {number} x */
export function indexAt(xs, x) {
  if (!xs.length) return -1;
  let low = 0, high = xs.length - 1;
  while (low < high) {
    const middle = (low + high) >> 1;
    if (xs[middle] < x) low = middle + 1;
    else high = middle;
  }
  // xs[low] is the first value >= x; the one before may be nearer. Equal values keep the first.
  const first = xs.indexOf(xs[low]);
  return first > 0 && x - xs[first - 1] <= xs[first] - x ? xs.indexOf(xs[first - 1]) : first;
}

/**
 * The series a track can draw, in reading order; a channel the file did not record is absent.
 * @param {TrackSamples} samples @param {boolean} paced speed as a pace instead of km/h @returns {Series[]}
 */
export function seriesOf(samples, paced) {
  /** @type {Series[]} */
  const out = [];
  if (present(samples.speed)) {
    const values = (samples.speed ?? []).map(paced ? pace : kmh);
    if (present(values)) out.push({ key: paced ? 'pace' : 'speed', values, inverted: paced });
  }
  for (const key of CHANNELS) {
    if (present(samples[key])) out.push({ key, values: /** @type {Column} */ (samples[key]), inverted: false });
  }
  return out;
}

/** Timer pauses as [start, end] seconds; a pause never resumed ends with the track. @param {Track['events']} events @param {number} end */
export function pauses(events, end) {
  /** @type {[number, number][]} */
  const out = [];
  /** @type {number | null} */
  let since = null;
  events.time.forEach((time, i) => {
    if (events.action[i] === 'pause' && since === null) since = time;
    else if (events.action[i] === 'resume' && since !== null) {
      if (time > since) out.push([since, time]);
      since = null;
    }
  });
  if (since !== null && end > since) out.push([since, end]);
  return out;
}

/** Number of laps (every lap column has the same length). @param {TrackLaps} laps */
export const lapCount = laps => (laps.time ?? laps.start_time ?? []).length;

/** Sample indexes where a lap ends and the next begins. @param {TrackLaps} laps @returns {number[]} */
export function lapBoundaries(laps) {
  const starts = laps.start_index ?? [];
  return starts.slice(1).filter(/** @returns {index is number} */ index => index != null);
}

/**
 * Average and maximum heart rate: from the laps when every lap has them (the device's own values),
 * else time-weighted over the samples, which may be thinned.
 * @param {Track} track @returns {{average: number | null, maximum: number | null}}
 */
export function heartRate(track) {
  const { laps, samples } = track;
  const averages = laps.average_heart_rate ?? [], weights = laps.moving_seconds ?? laps.elapsed_seconds ?? [];
  if (lapCount(laps) && averages.every(value => value != null) && weights.every(value => value != null && value > 0)) {
    let total = 0, sum = 0;
    weights.forEach((weight, i) => { total += Number(weight); sum += Number(averages[i]) * Number(weight); });
    const average = sum / total;
    const maxima = (laps.maximum_heart_rate ?? []).filter(value => value != null);
    return { average, maximum: maxima.length ? Math.max(.../** @type {number[]} */ (maxima)) : null };
  }
  return weightedHeartRate(samples);
}

/** @param {TrackSamples} samples */
function weightedHeartRate(samples) {
  const rates = samples.heart_rate ?? [], times = samples.time;
  let sum = 0, span = 0, maximum = -Infinity;
  rates.forEach((rate, i) => {
    if (rate == null) return;
    maximum = Math.max(maximum, rate);
    const step = (times[i + 1] ?? times[i]) - times[i];
    sum += rate * step;
    span += step;
  });
  if (maximum === -Infinity) return { average: null, maximum: null };
  return { average: span > 0 ? sum / span : maximum, maximum };
}

/** First and last sample with a position, or null for a track without any. @param {TrackSamples} samples */
export function routeEnds(samples) {
  const { latitude = [], longitude = [] } = samples;
  const valid = (/** @type {number} */ i) => latitude[i] != null && longitude[i] != null;
  let first = -1, last = -1;
  for (let i = 0; i < latitude.length; i++) if (valid(i)) { if (first < 0) first = i; last = i; }
  return first < 0 ? null : { first, last };
}

/**
 * The route as runs of [longitude, latitude] with their sample indexes; a missing position cuts it.
 * @param {TrackSamples} samples @returns {{points: [number, number][], indexes: number[]}[]}
 */
export function routeRuns(samples) {
  const { latitude = [], longitude = [] } = samples;
  /** @type {{points: [number, number][], indexes: number[]}[]} */
  const runs = [];
  /** @type {{points: [number, number][], indexes: number[]} | null} */
  let run = null;
  for (let i = 0; i < latitude.length; i++) {
    const lat = latitude[i], lon = longitude[i];
    if (lat == null || lon == null) { run = null; continue; }
    if (!run) runs.push(run = { points: [], indexes: [] });
    run.points.push([lon, lat]);
    run.indexes.push(i);
  }
  return runs;
}

/**
 * The sample whose projected point is nearest (x, y), within `reach` (same units), or -1.
 * @param {([number, number] | null)[]} points projected points by sample index @param {number} x @param {number} y @param {number} reach
 */
export function nearestPoint(points, x, y, reach) {
  let best = -1, distance = reach * reach;
  points.forEach((point, i) => {
    if (!point) return;
    const d = (point[0] - x) ** 2 + (point[1] - y) ** 2;
    if (d < distance) { distance = d; best = i; }
  });
  return best;
}

/**
 * Equirectangular projection for a small area: x east, y south, both in metres from the west/north
 * edge of `bounds` ([south, west, north, east]). Good enough to draw a route without a map.
 * @param {number[]} bounds @returns {(longitude: number, latitude: number) => [number, number]}
 */
export function flatProjection(bounds) {
  const [, west, north] = bounds;
  const middle = ((bounds[0] + bounds[2]) / 2) * (Math.PI / 180);
  const metres = 6371008.8 * (Math.PI / 180);
  return (longitude, latitude) => [(longitude - west) * Math.cos(middle) * metres, (north - latitude) * metres];
}

/** Whether `inner` ([south, west, north, east]) lies inside `outer`. @param {number[]} inner @param {number[]} outer */
export const covers = (outer, inner) =>
  inner[0] >= outer[0] && inner[1] >= outer[1] && inner[2] <= outer[2] && inner[3] <= outer[3];
