// Keep the HTML silhouette until after a paint and the model enters the viewport.
// A bounded fallback also covers background tabs and broken observers; a model on a page or tab
// that is not shown (no layout box) waits for it, because the observer reports when it appears.
// Leaving the page disposes the WebGL model and shows the silhouette again; a page restored from
// the back/forward cache waits for a paint and visibility once more and builds a fresh model.
import { queries } from './breakpoints.js';

const FALLBACK_MS = 5000;

/** @typedef {import('./body.js').BodyViewer} BodyViewer */
/** @typedef {import('./body-metrics.js').BodyMetrics} BodyMetrics */

/**
 * Waits for a paint and for the container to enter the viewport, then calls `start`.
 * @param {HTMLElement} container
 * @param {{waiting: () => boolean, start: () => void}} parts
 */
function createViewportWait(container, { waiting, start }) {
  const narrow = matchMedia(queries.overviewTabs);
  const hasBox = () => container.getClientRects().length > 0;
  let frame = 0, intersecting = false;
  /** @type {ReturnType<typeof setTimeout> | undefined} */
  let fallback;
  /** @type {IntersectionObserver | undefined} */
  let observer;
  // An observer entry may describe the previous layout, before a card was hidden.
  // On phones neither that entry nor the safety timer may fetch an unseen figure.
  function startWhenShown() {
    if (narrow.matches && (document.hidden || !hasBox() || (observer && !intersecting))) return false;
    start();
    return true;
  }
  function observe() {
    if (!waiting()) return;
    try {
      observer = new IntersectionObserver(entries => {
        intersecting = entries.some(entry => entry.isIntersecting);
        if (!document.hidden && intersecting) startWhenShown();
      });
      observer.observe(container);
    } catch {
      startWhenShown(); // No observer: an unseen phone figure keeps waiting on the timer.
    }
  }
  function onFallback() {
    // Keep checking when the observer is unavailable or the browser tab is hidden.
    // Desktop retains its bounded background-tab fallback.
    if (!hasBox() || !startWhenShown()) fallback = setTimeout(onFallback, FALLBACK_MS);
  }
  return {
    arm() {
      fallback = setTimeout(onFallback, FALLBACK_MS);
      frame = requestAnimationFrame(() => { frame = requestAnimationFrame(observe); });
    },
    stop() {
      clearTimeout(fallback);
      cancelAnimationFrame(frame);
      observer?.disconnect();
    },
    /** A restored page has not been seen yet. */
    forget() { intersecting = false; },
    startIfSeen() { if (!document.hidden && intersecting) startWhenShown(); },
  };
}

/**
 * @param {HTMLElement} container
 * @param {{getMotion: () => boolean, load?: () => Promise<{createBody: (container: HTMLElement, motion: boolean) => BodyViewer}>}} options
 */
export function setupBodyLoader(container, { getMotion, load = () => import('./body.js') }) {
  /** @type {BodyViewer | null} */
  let body = null;
  /** @type {BodyMetrics | null} */
  let metrics = null;
  let started = false, disposed = false, failed = false;
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  const motion = () => getMotion() && !reduced.matches && !document.hidden && container.getClientRects().length > 0;
  const updateMotion = () => body?.setMotion(motion());
  const wait = createViewportWait(container, { waiting: () => !started && !disposed, start });
  async function start() {
    if (started || disposed) return;
    started = true;
    wait.stop();
    try {
      const { createBody } = await load();
      if (disposed) return;
      body = createBody(container, motion());
      if (metrics) body.setBodyMetrics(metrics);
      updateMotion();
    } catch {
      failed = true;
      container.dataset.modelFailed = 'true';
    }
  }
  function onVisibility() {
    updateMotion();
    wait.startIfSeen();
  }
  function arm() {
    wait.arm();
    document.addEventListener('visibilitychange', onVisibility);
    reduced.addEventListener('change', updateMotion);
  }
  function suspend() {
    if (disposed) return;
    disposed = true;
    wait.stop();
    document.removeEventListener('visibilitychange', onVisibility);
    reduced.removeEventListener('change', updateMotion);
    body?.dispose();
    body = null;
    // The disposed model removed its canvas; uncover the silhouette underneath.
    delete container.dataset.modelLoaded;
  }
  /** @param {PageTransitionEvent} event */
  function resume(event) {
    if (!event?.persisted || !disposed) return;
    disposed = false;
    // A failed import is not retried; the silhouette stays.
    if (failed) return;
    started = false;
    wait.forget();
    arm();
  }
  arm();
  window.addEventListener('pagehide', suspend);
  window.addEventListener('pageshow', resume);
  return {
    /** @param {BodyMetrics} value */
    setBodyMetrics(value) { metrics = value; body?.setBodyMetrics(value); },
    updateMotion,
  };
}
