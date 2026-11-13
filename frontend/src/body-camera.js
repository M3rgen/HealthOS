// Fits the camera to the current silhouette: every point of every height band, at any turn angle,
// must land inside the canvas minus the margins. Solved by bisection on camera distance.
import { MathUtils, Vector3 } from 'three';
import { BASE_HEIGHT_M, SHAPE_BINS } from './body-shape.js';

// Height cue: the frame always spans at least a 182 cm figure, so shorter people read shorter
// inside the same frame; beyond that the frame follows the figure so nothing is cropped.
const FRAME_MIN_SCALE = 1.04;
const MARGIN_Y = .07; // fraction of the canvas kept free above the head and below the feet
const MARGIN_X = .03;
const ELEVATION = MathUtils.degToRad(4); // camera looks very slightly down
const forward = new Vector3(), up = new Vector3();

/**
 * Places `camera` for a figure of `bands` scaled by `scale`; returns the camera's distance.
 * @param {import('./body-types').Camera} camera three.js PerspectiveCamera
 * @param {{bands: ReturnType<typeof import('./body-shape.js').createShape>['bands'], scale: number}} figure
 * @param {number} width @param {number} height @param {number} fov
 */
export function fitCamera(camera, { bands, scale }, width, height, fov) {
  const aspect = width / height;
  const tanY = Math.tan(MathUtils.degToRad(fov / 2)) * (1 - 2 * MARGIN_Y);
  const tanX = Math.tan(MathUtils.degToRad(fov / 2)) * aspect * (1 - 2 * MARGIN_X);
  const girth = Math.sqrt(scale);
  const figureTop = bands.top * scale;
  const frameTop = Math.max(figureTop, BASE_HEIGHT_M * FRAME_MIN_SCALE);
  const centerY = frameTop / 2;
  const points = [[0, 0], [0, frameTop]]; // [radius, height]; frame corners keep the height cue
  for (let b = 0; b < SHAPE_BINS; b++) {
    if (bands.rho[b] < 0) continue;
    points.push([bands.rho[b] * girth, bands.lo[b] * scale], [bands.rho[b] * girth, bands.hi[b] * scale]);
  }
  forward.set(0, -Math.sin(ELEVATION), -Math.cos(ELEVATION));
  up.set(0, Math.cos(ELEVATION), -Math.sin(ELEVATION));
  /** @param {number} distance */
  const fits = (distance) => {
    const camY = centerY + distance * Math.sin(ELEVATION), camZ = distance * Math.cos(ELEVATION);
    for (const [rho, y] of points) {
      const dy = y - camY;
      // Worst case over the turn: the point may sit anywhere on its circle of radius rho.
      for (let k = 0; k < 16; k++) {
        const angle = k / 16 * Math.PI * 2;
        const px = rho * Math.cos(angle), pz = rho * Math.sin(angle) - camZ;
        const depth = dy * forward.y + pz * forward.z;
        if (depth <= .05) return false;
        if (Math.abs(px) > tanX * depth || Math.abs(dy * up.y + pz * up.z) > tanY * depth) return false;
      }
    }
    return true;
  };
  let near = .5, far = 40;
  for (let i = 0; i < 28; i++) { const mid = (near + far) / 2; if (fits(mid)) far = mid; else near = mid; }
  camera.aspect = aspect;
  camera.position.set(0, centerY + far * Math.sin(ELEVATION), far * Math.cos(ELEVATION));
  camera.lookAt(0, centerY, 0);
  camera.updateProjectionMatrix();
  return far;
}
