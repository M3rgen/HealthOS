// The figure's body-shape animation: morph influences, height scale and the camera fit that
// follows them. Holds no renderer of its own; it only marks the scene dirty.
import { fitCamera } from './body-camera.js';
import { normalReliefScale } from './body-metrics.js';
import { FOV, PEDESTAL_WIDTH } from './body-scene.js';

const TWEEN_MS = 600;
const NOT_RUNNING = -1, WAITING_FOR_FRAME = -2;
/** @type {readonly ('fat'|'muscle'|'scale')[]} */
const KEYS = ['fat', 'muscle', 'scale'];

/** @param {number} t */
const ease = t => (t < .5 ? 4 * t * t * t : 1 - (-2 * t + 2) ** 3 / 2);

/**
 * @param {object} options
 * @param {HTMLElement} options.container
 * @param {import('./body-types').Stage} options.stage objects of body-scene.js
 * @param {import('./body-model.js').Figure} options.parts filled by prepareModel
 * @param {{dirty: boolean}} options.flags
 * @param {typeof import('./body-metrics.js').DEFAULT_NORMAL_RELIEF} options.defaultRelief
 */
export function createMorphAnimator({ container, stage, parts, flags, defaultRelief }) {
  const { camera, ground, pedestal, figure } = stage;
  const current = { fat: 0, muscle: 0, scale: 1 };
  let from = { ...current }, to = { ...current }, tweenStart = NOT_RUNNING;
  let relief = defaultRelief;

  const influences = () => ({
    fat_high: Math.max(current.fat, 0), fat_low: Math.max(-current.fat, 0),
    muscle_high: Math.max(current.muscle, 0), muscle_low: Math.max(-current.muscle, 0),
  });

  function apply() {
    const values = /** @type {Record<string, number>} */ (influences());
    for (const { mesh, indices } of parts.bindings) {
      for (const [name, index] of indices) mesh.morphTargetInfluences[index] = values[name];
    }
    const strength = normalReliefScale(values.fat_high, values.fat_low, relief);
    for (const [material, base] of parts.reliefMaterials) material.normalScale.copy(base).multiplyScalar(strength);
    parts.shape.measure(values);
    const girth = Math.sqrt(current.scale);
    figure.scale.set(girth, current.scale, girth);
    ground.scale.setScalar(girth);
    container.dataset.morphFat = current.fat.toFixed(3);
    container.dataset.morphMuscle = current.muscle.toFixed(3);
    container.dataset.heightScale = current.scale.toFixed(3);
    container.dataset.normalScale = strength.toFixed(3);
    flags.dirty = true;
  }

  function fitView() {
    const w = container.clientWidth, h = container.clientHeight;
    if (!w || !h) return;
    const distance = fitCamera(camera, { bands: parts.shape.bands, scale: current.scale }, w, h, FOV);
    // Keep the pedestal's 2:1 screen ellipse: depth on the ground = width / (2 · sin(view angle)).
    const viewAngle = Math.max(Math.atan2(camera.position.y, camera.position.z), .05);
    pedestal.scale.set(PEDESTAL_WIDTH, Math.min(PEDESTAL_WIDTH / (2 * Math.sin(viewAngle)), distance * 1.2), 1);
    flags.dirty = true;
  }

  return {
    fitView,
    /** @param {typeof relief} value */
    setRelief(value) { relief = value; },
    refresh() { apply(); fitView(); },
    /** Advances a running change of shape by one animation frame. @param {number} timestamp */
    step(timestamp) {
      if (tweenStart === WAITING_FOR_FRAME) { tweenStart = timestamp; return; }
      if (tweenStart < 0) return;
      const k = ease(Math.min(1, (timestamp - tweenStart) / TWEEN_MS));
      for (const key of KEYS) current[key] = from[key] + (to[key] - from[key]) * k;
      if (k >= 1) tweenStart = NOT_RUNNING;
      apply();
      fitView();
    },
    /** @param {{fat: number, muscle: number, scale: number}} target @param {boolean} animate */
    moveTo(target, animate) {
      to = target;
      if (to.fat === current.fat && to.muscle === current.muscle && to.scale === current.scale) return;
      if (!animate) {
        Object.assign(current, to); tweenStart = NOT_RUNNING; apply(); fitView();
        return;
      }
      from = { ...current };
      tweenStart = WAITING_FOR_FRAME; // starts on the next animation frame's timestamp
    },
  };
}
