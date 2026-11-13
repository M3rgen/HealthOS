// Synthetic records only: device Brussels, selected display and entry zone Istanbul.
const assert = require('node:assert/strict');
const { BASE, launch, run } = require('./lib.cjs');
const { mock, contained } = require('./helpers/accessibility.cjs');

run(async () => {
  const browser = await launch();
  try {
    for (const [width, height] of [[1366, 768], [390, 844]]) {
      const page = await browser.newPage({ viewport: { width, height }, timezoneId: 'Europe/Brussels', reducedMotion: 'reduce' });
      const errors = [], measurements = [], workouts = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.clock.setFixedTime(new Date('2026-11-11T12:34:00Z'));
      await page.addInitScript(() => localStorage.setItem('healthos.timezone.v2', 'Europe/Istanbul'));
      await mock(page);
      await page.route('**/api/dashboard/measurements', async route => {
        if (route.request().method() === 'POST') {
          measurements.push({ ...route.request().postDataJSON(), id: 'zone-measurement', source: 'Manuel', flags: [] });
          return route.fulfill({ status: 201, json: measurements[0] });
        }
        return route.fulfill({ json: measurements });
      });
      await page.route('**/api/dashboard/workouts**', async route => {
        if (route.request().method() === 'POST') {
          const body = route.request().postDataJSON();
          workouts.push({ ...body, id: 'zone-workout', source: 'Manuel', duration_seconds: (Date.parse(body.end_time) - Date.parse(body.start_time)) / 1000 });
          return route.fulfill({ status: 201, json: workouts[0] });
        }
        return route.fulfill({ json: workouts });
      });
      await page.goto(BASE);
      await page.waitForFunction(() => document.documentElement.dataset.booted === 'true');
      await page.locator('[data-page="measurements"]').click();
      await page.locator('#measurements-add').click();
      const defaults = {
        value: await page.locator('#measurement-date').inputValue(),
        max: await page.locator('#measurement-date').getAttribute('max'),
        zone: await page.locator('#input-timezone').textContent(),
      };
      await page.locator('#measurement-value').fill('75');
      await page.locator('#measurement-date').fill('2026-11-10T08:00');
      await contained(page);
      await page.locator('#measurement-save').click();
      await page.locator('#measurement-dialog').waitFor({ state: 'hidden' });
      await page.locator('#measurement-rows [data-flag-id="zone-measurement"]').waitFor();
      const measurementTime = await page.locator('#measurement-rows tr').textContent();
      await contained(page);

      await page.locator('[data-page="sports"]').click();
      await page.waitForFunction(() => document.querySelector('#workout-timeline').getAttribute('aria-busy') === 'false');
      await page.locator('#workout-add').click();
      await contained(page);
      const workoutZone = await page.locator('#workout-timezone').textContent();
      await page.locator('[name="exercise_type"]').fill('Sentetik yürüyüş');
      await page.locator('[name="start_time"]').fill('2026-11-10T08:00');
      await page.locator('[name="end_time"]').fill('2026-11-10T09:00');
      await contained(page);
      await page.locator('#workout-save').click();
      await page.locator('[data-workout-id="zone-workout"]').waitFor();
      const workoutTime = await page.locator('[data-workout-id="zone-workout"] .workout-time').textContent();
      await contained(page);
      console.log(`INFO ${width}px measurement POST=${measurements[0].measured_at} row=${measurementTime}; workout POST=${workouts[0].start_time}–${workouts[0].end_time} row=${workoutTime}`);
      assert.equal(measurements[0].measured_at, '2026-11-10T05:00:00.000Z');
      assert.match(measurementTime, /08:00/);
      assert.equal(workouts[0].start_time, '2026-11-10T05:00:00.000Z');
      assert.equal(workouts[0].end_time, '2026-11-10T06:00:00.000Z');
      assert.match(workoutTime, /08:00.*09:00/);
      assert.equal(defaults.value, '2026-11-11T15:34');
      assert.equal(defaults.max, defaults.value);
      assert.match(defaults.zone, /Europe\/Istanbul/);
      assert.match(workoutZone, /Europe\/Istanbul/);
      assert.deepEqual(errors, []);
      console.log(`PASS ${width}x${height}: selected-zone defaults, hints, POST instants, list times and no page overflow`);
      await page.close();
    }
  } finally { await browser.close(); }
});
