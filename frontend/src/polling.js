// A background job on a fixed interval. A failing tick is reported and the next one still runs.
// Ticks are skipped while paused (a hidden tab); catchUp() runs the missed one when the tab is
// shown again and the last run is older than the interval, so a returning reader never waits a
// minute for data that is already stale.
/**
 * @param {() => Promise<unknown>} tick
 * @param {{interval?: number, paused?: () => boolean, timers?: Pick<typeof globalThis, 'setInterval'|'clearInterval'>,
 *   report?: (error: unknown) => void, now?: () => number}} [options]
 */
export function createPolling(tick, { interval = 60000, paused = () => false, timers = globalThis, report = console.error, now = Date.now } = {}) {
  /** @type {ReturnType<typeof setInterval> | null} */
  let handle = null;
  let last = now();
  const run = async () => {
    if (paused()) return;
    last = now();
    try {
      await tick();
    } catch (error) {
      report(error);
    }
  };
  const polling = {
    start() {
      if (handle !== null) timers.clearInterval(handle);
      handle = timers.setInterval(run, interval);
    },
    stop() {
      if (handle !== null) timers.clearInterval(handle);
      handle = null;
    },
    /** Runs a tick now when a running poll missed one; the next tick then follows a full interval later. */
    catchUp() {
      if (handle === null || now() - last < interval) return;
      polling.start();
      run();
    },
    get running() {
      return handle !== null;
    },
  };
  return polling;
}
