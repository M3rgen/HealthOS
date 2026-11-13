import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createPolling } from '../../frontend/src/polling.js';

test('a tab shown again after a skipped tick catches up once, then waits a full interval', async () => {
  let clock = 0, runs = 0, hidden = false, timer = null, starts = 0;
  const polling = createPolling(async () => { runs++; }, {
    interval: 60000,
    paused: () => hidden,
    now: () => clock,
    timers: { setInterval: fn => { timer = fn; starts++; return starts; }, clearInterval: () => { timer = null; } },
  });
  polling.start();
  clock = 30000;
  polling.catchUp();
  assert.equal(runs, 0, 'nothing missed yet');
  hidden = true;
  clock = 60000; await timer();
  clock = 120000; await timer();
  assert.equal(runs, 0, 'hidden ticks are skipped');
  hidden = false;
  clock = 125000;
  polling.catchUp();
  assert.equal(runs, 1, 'the missed tick runs on return');
  assert.equal(starts, 2, 'the interval starts again from the catch-up');
  polling.catchUp();
  assert.equal(runs, 1, 'only once');
  polling.stop();
  clock = 999999;
  polling.catchUp();
  assert.equal(runs, 1, 'a stopped poll stays stopped');
});
