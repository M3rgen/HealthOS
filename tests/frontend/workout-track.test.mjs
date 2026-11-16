import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { test } from 'node:test';
import { tables } from '../../frontend/src/i18n/workout-detail.js';
import { headerBounds } from '../../frontend/src/workout-basemap.js';
import {
  axes, clock, covers, flatProjection, heartRate, indexAt, kmh, lapBoundaries, nearestPoint, pace, pacedSport, pauses,
  positions, routeEnds, routeRuns, seriesOf,
} from '../../frontend/src/workout-track.js';

// Every track here is the invented preview run around 0°/0° (frontend/preview/track-fixture.cjs).
const require = createRequire(import.meta.url);
const { workoutTrack } = require('../../frontend/preview/track-fixture.cjs');
const { syntheticPmtiles, SYNTHETIC_AREA } = require('../../frontend/preview/synthetic-tiles.cjs');

test('speed reads as km/h or as a pace, and standing still has no pace', () => {
  assert.equal(kmh(2.5), 9);
  assert.equal(kmh(null), null);
  assert.equal(pace(2.5), 400);
  assert.equal(clock(pace(2.5)), '6:40');
  for (const still of [0, -1, null, undefined, 0.5]) assert.equal(pace(still), null, `pace(${still})`);
  assert.equal(pace(1000 / 1800), 1800);
});

test('clock shows minutes:seconds, hours only when there are some', () => {
  assert.deepEqual([0, 59.6, 400, 3599, 3725, null].map(clock), ['0:00', '1:00', '6:40', '59:59', '1:02:05', '—']);
});

test('runs and walks show pace first; other sports and typed names speed', () => {
  assert.deepEqual(['RUNNING', 'WALKING', 'HIKING', 'TREADMILL_RUNNING', 'CYCLING', 'Koşu', null].map(pacedSport),
    [true, true, true, true, false, false, false]);
});

test('distance is an axis only with an odometer that moves', () => {
  assert.deepEqual(axes({ time: [0, 1] }), ['time']);
  assert.deepEqual(axes({ time: [0, 1, 2], distance: [null, 5, 5] }), ['time']);
  assert.deepEqual(axes({ time: [0, 1, 2], distance: [null, 5, 9] }), ['time', 'distance']);
});

test('distance positions fill gaps forward and never decrease', () => {
  const samples = { time: [0, 4, 8, 12, 16], distance: [null, 5, null, 12, 11] };
  assert.deepEqual(positions(samples, 'distance'), [5, 5, 5, 12, 12]);
  assert.equal(positions(samples, 'time'), samples.time);
});

test('indexAt finds the nearest sample; ties and repeated values keep the earlier one', () => {
  const xs = [0, 4, 8, 12];
  assert.deepEqual([-3, 0, 1.9, 2, 2.1, 6, 7, 11, 99].map(x => indexAt(xs, x)), [0, 0, 0, 0, 1, 1, 2, 3, 3]);
  assert.equal(indexAt([0, 4, 4, 4, 8], 4.1), 1);
  assert.equal(indexAt([0, 4, 4, 4, 8], 6.5), 4);
  assert.equal(indexAt([], 3), -1);
});

// The API thins long tracks (LTTB): samples are uneven in time and distance. A chart position, a
// sample index and a route point must still name the same row in both directions.
function thinned() {
  const { track } = workoutTrack('thin');
  const keep = track.samples.time.map((_, i) => i).filter(i => i % 3 === 0 || i % 7 === 0 || i === track.samples.time.length - 1);
  const samples = Object.fromEntries(Object.entries(track.samples).map(([key, column]) => [key, keep.map(i => column[i])]));
  return { ...track, samples };
}

test('on a thinned track every chart position maps to its own sample and back to its route point', () => {
  const track = thinned();
  const project = flatProjection(track.summary.bounds);
  const points = track.samples.latitude.map((lat, i) => (lat == null ? null : project(track.samples.longitude[i], lat)));
  for (const axis of axes(track.samples)) {
    const xs = positions(track.samples, axis);
    xs.forEach((x, i) => {
      const index = indexAt(xs, x);
      assert.equal(xs[index], x, `${axis} ${i}`);
      if (xs.indexOf(x) === i) assert.equal(index, i, `${axis} ${i}`);
    });
  }
  points.forEach((point, i) => {
    if (point) assert.equal(nearestPoint(points, point[0] + 0.3, point[1] - 0.3, 5), i);
  });
  assert.equal(nearestPoint(points, -1e6, -1e6, 30), -1, 'a tap far from the route picks nothing');
});

test('series: speed or pace first, then only the channels the file recorded', () => {
  const { samples } = workoutTrack('s').track;
  assert.deepEqual(seriesOf(samples, true).map(series => [series.key, series.inverted]),
    [['pace', true], ['heart_rate', false], ['altitude', false]]);
  assert.deepEqual(seriesOf(samples, false).map(series => series.key), ['speed', 'heart_rate', 'altitude']);
  assert.deepEqual(seriesOf({ time: [0, 1], speed: [0, null], cadence: [null, null] }, true), []);
  assert.deepEqual(seriesOf({ time: [0, 1], speed: [0, 3] }, true)[0].values, [null, 1000 / 3]);
});

test('pauses pair pause with resume; one never resumed lasts to the end', () => {
  const events = { time: [10, 40, 50, 55, 70, 90], action: ['resume', 'pause', 'pause', 'resume', 'pause', 'resume'] };
  assert.deepEqual(pauses(events, 100), [[40, 55], [70, 90]]);
  assert.deepEqual(pauses({ time: [80], action: ['pause'] }, 100), [[80, 100]]);
  assert.deepEqual(pauses({ time: [], action: [] }, 100), []);
});

test('laps give their boundaries, and heart rate from the laps when each has one', () => {
  const { track } = workoutTrack('l');
  assert.deepEqual(lapBoundaries(track.laps), track.laps.start_index.slice(1));
  const fromLaps = heartRate(track);
  assert.equal(fromLaps.maximum, Math.max(...track.laps.maximum_heart_rate));
  const weights = track.laps.moving_seconds, total = weights.reduce((a, b) => a + b, 0);
  assert.equal(fromLaps.average, track.laps.average_heart_rate.reduce((sum, v, i) => sum + v * weights[i], 0) / total);
  const fromSamples = heartRate({ ...track, laps: {}, samples: { time: [0, 10, 30], heart_rate: [100, 160, 170] } });
  assert.deepEqual(fromSamples, { average: (100 * 10 + 160 * 20) / 30, maximum: 170 });
  assert.deepEqual(heartRate({ ...track, laps: {}, samples: { time: [0] } }), { average: null, maximum: null });
});

test('the route breaks where a position is missing and ends at the last valid one', () => {
  const samples = { time: [0, 1, 2, 3, 4, 5], latitude: [null, 0.1, 0.2, null, 0.3, null], longitude: [null, 0.4, 0.5, null, 0.6, 0.7] };
  assert.deepEqual(routeRuns(samples), [{ points: [[0.4, 0.1], [0.5, 0.2]], indexes: [1, 2] }, { points: [[0.6, 0.3]], indexes: [4] }]);
  assert.deepEqual(routeEnds(samples), { first: 1, last: 4 });
  assert.equal(routeEnds({ time: [0] }), null);
});

test('the basemap header gives the area it covers; anything else is no basemap', () => {
  const archive = syntheticPmtiles();
  const header = archive.buffer.slice(archive.byteOffset, archive.byteOffset + 127);
  const [west, south, east, north] = SYNTHETIC_AREA;
  assert.deepEqual(headerBounds(header), [south, west, north, east]);
  assert.equal(headerBounds(new ArrayBuffer(127)), null);
  assert.equal(headerBounds(header.slice(0, 100)), null);
  const { bounds } = workoutTrack('c').track.summary;
  assert.equal(covers(headerBounds(header), bounds), true);
  assert.equal(covers(headerBounds(header), [bounds[0], bounds[1], 0.5, bounds[3]]), false);
});

test('the detail view says the same things in Turkish and English', () => {
  const shape = value => (value && typeof value === 'object' ? Object.fromEntries(Object.entries(value).map(([k, v]) => [k, shape(v)])) : typeof value);
  assert.deepEqual(shape(tables.en), shape(tables.tr));
});
