// The 3D body viewer: owns the WebGL renderer and its loop, loads the model and animates morphs,
// camera fit and rotation. A lost GL context shows the silhouette until the browser restores it.
import { NeutralToneMapping, SRGBColorSpace, VSMShadowMap, WebGLRenderer } from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { MeshoptDecoder } from 'three/addons/libs/meshopt_decoder.module.js';
import { createSkinDetail } from './body-detail.js';
import { metricsToMorphs, DEFAULT_NORMAL_RELIEF } from './body-metrics.js';
import { MORPHS, prepareModel } from './body-model.js';
import { createMorphAnimator } from './body-morphs.js';
import { createStage } from './body-scene.js';
import { createShape } from './body-shape.js';
import { createSkinUniforms } from './body-skin.js';
import './body.css';

// Preserve the public viewer API while keeping profile calculations independent of three.js.
export { metricsToMorphs, DEFAULT_NORMAL_RELIEF, normalReliefScale } from './body-metrics.js';

// Locally bundled body model (see public/models/PROVENANCE.md). glTF Y-up, feet at y=0,
// facing +Z, ~1.75 m at basis, carrying the morph targets below on every deforming mesh.
// Meshopt-compressed (EXT_meshopt_compression + KHR_mesh_quantization, WebP normal map); the file
// name carries a content hash so /models can be cached as immutable. Rename it when the model changes.
const MODEL_URL = '/models/body.d736859b.glb';
const START_TURN = .35; // front three-quarter view (radians)

const SILHOUETTE = 'M60 6c-10 0-17 8-17 20 0 10 4 18 8 22l-2 10c-14 3-23 11-26 26l-9 58-1 29c0 7 5 9 7 3l4-26 10-40 5 43-6 76-1 34-7 8c-3 4 0 7 7 7h10'
  + 'l8-9 3-38 7-45 7 45 3 38 8 9h10c7 0 10-3 7-7l-7-8-1-34-6-76 5-43 10 40 4 26c2 6 7 4 7-3l-1-29-9-58c-3-15-12-23-26-26l-2-10c4-4 8-12 8-22 0-12-7-20-17-20Z';

/** @typedef {import('./body-metrics.js').BodyMetrics} BodyMetrics */
/** @typedef {{setMotion: (value: boolean) => void, setBodyMetrics: (metrics: BodyMetrics) => void, dispose: () => void}} BodyViewer */
/** @typedef {{disposed: boolean, loaded: boolean, contextLost: boolean, dirty: boolean}} ViewerFlags */
/** @typedef {ReturnType<typeof createStage>} Stage */
/** @typedef {ReturnType<typeof createMorphAnimator>} MorphAnimator */

/** @param {HTMLElement} container @returns {BodyViewer} */
function staticFallback(container) {
  const fallback = document.createElement('div');
  fallback.className = 'model-fallback';
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 120 280');
  svg.setAttribute('aria-hidden', 'true');
  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('fill', '#8d8b85');
  path.setAttribute('d', SILHOUETTE);
  svg.append(path);
  fallback.append(svg);
  container.replaceChildren(fallback);
  return { setMotion() {}, setBodyMetrics() {}, dispose() {} };
}

/** @param {import('./body-types').Object3D} root a three.js object */
function disposeTree(root) {
  root.traverse((/** @type {import('./body-types').ModelNode} */ node) => {
    if (!node.isMesh) return;
    node.geometry.dispose();
    for (const material of [node.material].flat()) {
      for (const value of Object.values(material))
        if ((/** @type {Partial<import('./body-types').Texture> | null} */ (value))?.isTexture) /** @type {import('./body-types').Texture} */ (value).dispose();
      material.dispose();
    }
  });
}

const pixelRatio = () => Math.min(window.devicePixelRatio || 1, 1.75);

/** @param {import('./body-types').Renderer} renderer three.js WebGLRenderer */
function configureRenderer(renderer) {
  renderer.setPixelRatio(pixelRatio());
  renderer.setClearColor(0x000000, 0);
  renderer.outputColorSpace = SRGBColorSpace;
  // Khronos PBR Neutral keeps the painted skin hue (ACES pushed it orange); exposure keeps forehead and
  // shoulder highlights below clipping while the shadow side stays lifted off the #181814 page.
  renderer.toneMapping = NeutralToneMapping;
  renderer.toneMappingExposure = .88;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = VSMShadowMap; // smooth penumbrae; jittered PCF combed grazing skin
}

/**
 * Fetches the figure. A missing or invalid model calls onFailed instead (the silhouette stays).
 * @param {object} parts
 * @param {HTMLElement} parts.container
 * @param {Stage} parts.stage
 * @param {import('./body-types').SkinUniforms} parts.skinUniforms
 * @param {import('./body-model.js').Figure} parts.parts
 * @param {ViewerFlags} parts.flags
 * @param {MorphAnimator} parts.morphs
 * @param {() => void} parts.onFailed
 */
function loadFigure({ container, stage, skinUniforms, parts, flags, morphs, onFailed }) {
  const { contacts, normalizer } = stage;
  // The meshopt decoder is bundled from three (WebAssembly, so the CSP needs 'wasm-unsafe-eval').
  new GLTFLoader().setMeshoptDecoder(MeshoptDecoder).load(MODEL_URL, (/** @type {{scene: import('./body-types').Object3D}} */ { scene: model }) => {
    if (flags.disposed) { disposeTree(model); return; }
    const { relief, found } = prepareModel(model, { normalizer, contacts, skinUniforms, figure: parts });
    morphs.setRelief(relief);
    container.dataset.model = 'body';
    container.dataset.morphTargets = [...found].join(',');
    if (found.size !== MORPHS.length) {
      console.warn(`body.glb is missing morph targets: ${MORPHS.filter(name => !found.has(name)).join(', ')}`);
    }
    normalizer.add(model);
    flags.loaded = true;
    morphs.refresh();
    container.dataset.modelLoaded = 'true';
  }, undefined, () => {
    if (!flags.disposed) onFailed();
  });
}

/**
 * The canvas goes blank while the GPU context is gone; the HTML silhouette underneath stands in.
 * @param {import('./body-types').Renderer} renderer three.js WebGLRenderer @param {HTMLElement} container @param {Stage} stage @param {ViewerFlags} flags
 */
function watchContext(renderer, container, stage, flags) {
  /** @param {Event} event */
  function onLost(event) {
    event.preventDefault(); // Without this the browser never restores the context.
    flags.contextLost = true;
    flags.dirty = true;
    delete container.dataset.modelLoaded;
  }
  function onRestored() {
    flags.contextLost = false;
    stage.renewEnvironment();
    if (flags.loaded) container.dataset.modelLoaded = 'true';
    flags.dirty = true;
  }
  renderer.domElement.addEventListener('webglcontextlost', onLost);
  renderer.domElement.addEventListener('webglcontextrestored', onRestored);
  return () => {
    renderer.domElement.removeEventListener('webglcontextlost', onLost);
    renderer.domElement.removeEventListener('webglcontextrestored', onRestored);
  };
}

/** Thirty seconds per half turn from a front three-quarter view, easing before returning. @param {number} elapsed */
const turnAngle = elapsed => START_TURN + Math.PI * (1 - Math.cos(elapsed / 60000 * Math.PI * 2)) / 2;

/**
 * Keeps the canvas at its container's size and the skin detail fitted to it.
 * @param {HTMLElement} container @param {import('./body-types').Renderer} renderer three.js WebGLRenderer
 * @param {MorphAnimator} morphs @param {ReturnType<typeof createSkinDetail>} skinDetail @param {ViewerFlags} flags
 */
function watchSize(container, renderer, morphs, skinDetail, flags) {
  skinDetail.fit(container.clientHeight * pixelRatio());
  const resize = new ResizeObserver(() => {
    const w = container.clientWidth, h = container.clientHeight;
    if (w && h) { renderer.setSize(w, h, false); morphs.fitView(); }
    skinDetail.fit(h * pixelRatio());
    flags.dirty = true; // Also stop/wake the loop when a tab hides/reveals the container.
  });
  resize.observe(container);
  return resize;
}

/**
 * The render loop and the figure's rotation. Auto-rotation runs only when the System-page toggle
 * is on and the OS does not ask for reduced motion; otherwise the figure rests in the front
 * three-quarter view.
 * @param {object} parts
 * @param {import('./body-types').Renderer} parts.renderer three.js WebGLRenderer
 * @param {Stage} parts.stage
 * @param {HTMLElement} parts.container
 * @param {ViewerFlags} parts.flags
 * @param {MorphAnimator} parts.morphs
 * @param {MediaQueryList} parts.reducedMotion
 * @param {boolean} parts.motionEnabled
 */
function startTurning({ renderer, stage, container, flags, morphs, reducedMotion, motionEnabled }) {
  const { scene, camera, turntable } = stage;
  let enabled = motionEnabled, elapsed = 0, last = 0, running = false, tweenUntil = 0, dirty = flags.dirty;
  const rotates = () => flags.loaded && enabled && !reducedMotion.matches;
  const visible = () => !flags.disposed && !flags.contextLost && !document.hidden && !!container.getClientRects().length;
  function syncLoop() {
    const next = visible() && (rotates() || dirty || tweenUntil !== 0);
    if (next === running) return;
    running = next; last = 0;
    renderer.setAnimationLoop(next ? frame : null);
  }
  /** @param {number} timestamp */
  function frame(timestamp) {
    if (!visible()) { syncLoop(); return; }
    const delta = last ? Math.min(timestamp - last, 100) : 0; last = timestamp;
    // A requested morph starts on its first frame and lasts 600 ms (body-morphs.js).
    // Keep its clock alive through the final frame, then release the animation loop entirely.
    if (tweenUntil === -1) tweenUntil = timestamp + 600;
    morphs.step(timestamp);
    const turning = rotates();
    if (turning) {
      elapsed += delta;
      turntable.rotation.y = turnAngle(elapsed);
      container.dataset.rotationDegrees = ((turntable.rotation.y - START_TURN) * 180 / Math.PI).toFixed(1);
    }
    if (turning || dirty) { renderer.render(scene, camera); flags.dirty = false; }
    if (timestamp >= tweenUntil) tweenUntil = 0;
    syncLoop();
  }
  function onReducedMotion() {
    if (reducedMotion.matches) { elapsed = 0; turntable.rotation.y = START_TURN; container.dataset.rotationDegrees = '0.0'; }
    flags.dirty = true;
  }
  // Async model/texture loads, camera fits and metric changes already mark this shared flag.
  // Turning it into an invalidation signal avoids an idle rAF polling that flag forever.
  Object.defineProperty(flags, 'dirty', { get: () => dirty, set(value) { dirty = value; if (value) syncLoop(); } });
  reducedMotion.addEventListener('change', onReducedMotion);
  document.addEventListener('visibilitychange', syncLoop);
  turntable.rotation.y = START_TURN;
  container.dataset.rotationDegrees = '0.0';
  syncLoop();
  return {
    /** @param {boolean} value */
    setEnabled(value) { enabled = value; flags.dirty = true; },
    animate() { tweenUntil = -1; syncLoop(); },
    dispose() {
      renderer.setAnimationLoop(null);
      reducedMotion.removeEventListener('change', onReducedMotion);
      document.removeEventListener('visibilitychange', syncLoop);
    },
  };
}

/** @param {HTMLElement} container @param {boolean} motionEnabled @returns {BodyViewer} */
export function createBody(container, motionEnabled) {
  /** @type {import('./body-types').Renderer} three.js WebGLRenderer */
  let renderer;
  try { renderer = new WebGLRenderer({ antialias: true, alpha: true, powerPreference: 'low-power' }); }
  catch { return staticFallback(container); }
  configureRenderer(renderer);
  container.append(renderer.domElement);

  const flags = { disposed: false, loaded: false, contextLost: false, dirty: true };
  const stage = createStage(renderer, { onPedestalLoaded: () => { flags.dirty = true; } });
  const { ground, turntable } = stage;

  // Shared skin shader uniforms (see makeSkinMaterial); detail maps arrive asynchronously.
  const skinUniforms = createSkinUniforms();
  /** @type {import('./body-model.js').Figure} */
  const parts = { skinMaterials: [], reliefMaterials: [], bindings: [], shape: createShape() }; // filled by prepareModel
  const skinDetail = createSkinDetail({ renderer, container, skinUniforms, parts, flags });
  const morphs = createMorphAnimator({ container, stage, parts, flags, defaultRelief: DEFAULT_NORMAL_RELIEF });
  const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');

  loadFigure({ container, stage, skinUniforms, parts, flags, morphs, onFailed() { dispose(); staticFallback(container); } });
  const stopWatchingContext = watchContext(renderer, container, stage, flags);
  const resize = watchSize(container, renderer, morphs, skinDetail, flags);
  const turning = startTurning({ renderer, stage, container, flags, morphs, reducedMotion, motionEnabled });

  function dispose() {
    if (flags.disposed) return;
    flags.disposed = true;
    renderer.setAnimationLoop(null);
    resize.disconnect();
    turning.dispose();
    stopWatchingContext();
    disposeTree(turntable);
    disposeTree(ground);
    stage.disposeEnvironment();
    skinDetail.textures.forEach(texture => texture.dispose());
    // dispose() alone leaves the WebGL context to the garbage collector, and a browser allows only
    // about 16 at once: back/forward restores build a new one each time.
    renderer.forceContextLoss();
    renderer.dispose();
    renderer.domElement.remove();
    window.removeEventListener('pagehide', dispose);
  }
  window.addEventListener('pagehide', dispose);

  return {
    /** @param {boolean} value */
    setMotion(value) { turning.setEnabled(value); flags.dirty = true; },
    /** Accepts {heightCm, weightKg, bodyFatPct, musclePct}; missing values keep the basis. @param {BodyMetrics} metrics */
    setBodyMetrics(metrics) {
      const animate = flags.loaded && !reducedMotion.matches;
      morphs.moveTo(metricsToMorphs(metrics), animate);
      if (animate) turning.animate();
    },
    dispose,
  };
}
