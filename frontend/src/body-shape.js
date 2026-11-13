// The silhouette of the current morph state: per height band, the widest reach from the turn
// axis. The camera fit uses it to frame the figure at any turn without cropping.
export const BASE_HEIGHT_M = 1.75;
export const SHAPE_BINS = 48;
const SHAPE_SPAN_M = 2.4; // normalised-model height covered by the silhouette bins

/** @param {number} v @param {number} lo @param {number} hi */
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

export function createShape() {
  const bands = { rho: new Float32Array(SHAPE_BINS), lo: new Float32Array(SHAPE_BINS), hi: new Float32Array(SHAPE_BINS), top: BASE_HEIGHT_M };
  /** @type {import('./body-model.js').ShapeMesh[]} */
  const meshes = []; // { base: Float32Array, deltas: [[morphName, Float32Array]] }, in normalised figure space
  let measured = '';
  /** @param {Record<string, number>} values morph influences by name */
  function measure(values) {
    const key = `${values.fat_high},${values.fat_low},${values.muscle_high},${values.muscle_low}`;
    if (key === measured || !meshes.length) return;
    measured = key;
    bands.rho.fill(-1); bands.lo.fill(Infinity); bands.hi.fill(-Infinity);
    let top = 0;
    const binHeight = SHAPE_SPAN_M / SHAPE_BINS;
    for (const { base, deltas } of meshes) {
      /** @type {[number, Float32Array][]} */
      const active = deltas.filter(([name]) => values[name] > 0).map(([name, delta]) => [values[name], delta]);
      for (let i = 0; i < base.length; i += 3) {
        let x = base[i], y = base[i + 1], z = base[i + 2];
        for (const [w, delta] of active) { x += w * delta[i]; y += w * delta[i + 1]; z += w * delta[i + 2]; }
        const bin = clamp(Math.floor(y / binHeight), 0, SHAPE_BINS - 1);
        const rho = Math.hypot(x, z);
        if (rho > bands.rho[bin]) bands.rho[bin] = rho;
        if (y < bands.lo[bin]) bands.lo[bin] = y;
        if (y > bands.hi[bin]) bands.hi[bin] = y;
        if (y > top) top = y;
      }
    }
    bands.top = top;
  }
  return { bands, meshes, measure };
}
