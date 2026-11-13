// Turns a loaded glTF scene into the HealthOS figure: fits it to the 1.75 m contract, swaps its
// materials for the skin material, records its morph bindings and silhouette data, and places the
// foot contact shadows. Runs once per model; the viewer in body.js only animates the result.
import { Box3, Color, Matrix3, Matrix4, Vector3 } from 'three';
import { DEFAULT_NORMAL_RELIEF, parseHealthos, reliefParams } from './body-metrics.js';
import { makeSkinMaterial } from './body-skin.js';
import { BASE_HEIGHT_M } from './body-shape.js';

export const MORPHS = ['fat_high', 'fat_low', 'muscle_high', 'muscle_low'];

/** @typedef {{base: Float32Array, deltas: [string, Float32Array][]}} ShapeMesh */
/**
 * @typedef {{skinMaterials: import('./body-types').Material[], reliefMaterials: [import('./body-types').Material, import('./body-types').Vector2][], bindings: {mesh:
 * import('./body-types').Mesh, indices: [string, number][]}[], shape: ReturnType<typeof import('./body-shape.js').createShape>}} Figure
 */

// Contact shadows from the lowest vertices, split at the stance midline. The standing foot (most
// vertices on the floor) gets the full shadow; a raised heel (contrapposto) only a faint one.
/** @param {import('./body-types').Decal[]} contacts the two foot decals @param {ShapeMesh[]} meshes */
function placeContacts(contacts, meshes) {
  /** @type {number[]} */
  const low = [];
  for (const { base } of meshes) for (let i = 0; i < base.length; i += 3) if (base[i + 1] < .045) low.push(base[i], base[i + 2], base[i + 1]);
  if (!low.length) return;
  let minX = Infinity, maxX = -Infinity;
  for (let i = 0; i < low.length; i += 3) { minX = Math.min(minX, low[i]); maxX = Math.max(maxX, low[i]); }
  const mid = (minX + maxX) / 2;
  const planted = [0, 0];
  for (let i = 0; i < low.length; i += 3) if (low[i + 2] < .006) planted[low[i] < mid ? 0 : 1]++;
  const standing = planted[0] >= planted[1] ? 0 : 1;
  contacts.forEach((decal, side) => {
    const box = [Infinity, -Infinity, Infinity, -Infinity];
    for (let i = 0; i < low.length; i += 3) {
      if ((low[i] < mid) !== (side === 0)) continue;
      box[0] = Math.min(box[0], low[i]); box[1] = Math.max(box[1], low[i]);
      box[2] = Math.min(box[2], low[i + 1]); box[3] = Math.max(box[3], low[i + 1]);
    }
    if (!Number.isFinite(box[0])) return;
    decal.position.set((box[0] + box[1]) / 2, .002, (box[2] + box[3]) / 2);
    decal.scale.set(box[1] - box[0] + .22, box[3] - box[2] + .22, 1);
    decal.material.opacity = side === standing ? 1 : .45;
    decal.visible = true;
  });
}

/** @param {unknown} raw the model's `extras.healthos`, unvalidated */
function readMetadata(raw) {
  const healthos = parseHealthos(raw);
  const hex = healthos?.skinToneHex ?? '';
  return { relief: reliefParams(healthos), skinTone: /^#?[0-9a-f]{6}$/i.test(hex) ? new Color(`#${hex.replace('#', '')}`) : null };
}

const DETAIL_DEFINES = [['SKIN_DETAIL_NORMAL', 'skinDetailNormal'], ['SKIN_DETAIL_ROUGHNESS', 'skinDetailRoughness']];

// Swaps one source material for the skin material and records it in the viewer's bookkeeping.
/**
 * @param {import('./body-types').Material} source @param {{skinUniforms: import('./body-types').SkinUniforms, figure: Figure, skinTone: import('./body-types').Color |
 * null}} parts
 */
function convertMaterial(source, { skinUniforms, figure, skinTone }) {
  const material = makeSkinMaterial(source, skinUniforms);
  if (skinTone && !material.map) material.color.copy(skinTone);
  for (const [define, uniform] of DETAIL_DEFINES) if (skinUniforms[uniform].value) material.defines[define] = '';
  if (material.normalMap) figure.reliefMaterials.push([material, material.normalScale.clone()]);
  figure.skinMaterials.push(material);
  source.dispose(); // textures are now owned by the physical material
  return material;
}

/** Sizes and centres `normalizer` so the model meets the 1.75 m contract. @param {import('./body-types').Object3D} model @param {import('./body-types').Object3D} normalizer */
function fitToContract(model, normalizer) {
  model.updateMatrixWorld(true);
  const box = new Box3();
  const point = new Vector3();
  model.traverse((/** @type {import('./body-types').ModelNode} */ node) => {
    if (!node.isMesh) return;
    const position = node.geometry.attributes.position;
    for (let i = 0; i < position.count; i++) box.expandByPoint(point.fromBufferAttribute(position, i).applyMatrix4(node.matrixWorld));
  });
  const size = box.getSize(new Vector3());
  // Contract says 1.75 m with feet at y=0; normalise anyway so any source unit still frames correctly.
  const unit = size.y > 1.4 && size.y < 2.1 ? 1 : BASE_HEIGHT_M / size.y;
  normalizer.scale.setScalar(unit);
  // A compliant model (feet at y=0, centre of mass on the origin) turns about its own origin, which
  // matters for asymmetric poses; only off-contract sources are re-centred on their bounding box.
  const compliant = unit === 1 && Math.abs(box.min.y) < .05 && Math.hypot((box.min.x + box.max.x) / 2, (box.min.z + box.max.z) / 2) < .25;
  if (compliant) normalizer.position.set(0, -box.min.y, 0);
  else normalizer.position.set(-(box.min.x + box.max.x) / 2 * unit, -box.min.y * unit, -(box.min.z + box.max.z) / 2 * unit);
  normalizer.updateMatrix();
}

/** The mesh's morph targets that the figure uses, as [name, index]. @param {import('./body-types').Mesh} node @returns {[string, number][]} */
function morphIndices(node) {
  const dict = node.morphTargetDictionary || {};
  return MORPHS.filter(name => name in dict).map(name => [name, dict[name]]);
}

/**
 * Copies positions and morph deltas into normalised figure space for silhouette measurement.
 * @param {import('./body-types').Mesh} node @param {[string, number][]} indices @param {import('./body-types').Object3D} normalizer
 * @returns {ShapeMesh}
 */
function silhouetteMesh(node, indices, normalizer) {
  const point = new Vector3(), delta = new Vector3();
  const toFigure = new Matrix4().multiplyMatrices(normalizer.matrix, node.matrixWorld);
  const linear = new Matrix3().setFromMatrix4(toFigure);
  const position = node.geometry.attributes.position;
  const targets = node.geometry.morphAttributes.position || [];
  const base = new Float32Array(position.count * 3);
  for (let i = 0; i < position.count; i++) point.fromBufferAttribute(position, i).applyMatrix4(toFigure).toArray(base, i * 3);
  const deltas = indices.map(([name, index]) => {
    const out = new Float32Array(position.count * 3);
    for (let i = 0; i < position.count; i++) {
      delta.fromBufferAttribute(targets[index], i);
      if (!node.geometry.morphTargetsRelative) delta.sub(point.fromBufferAttribute(position, i));
      delta.applyMatrix3(linear).toArray(out, i * 3);
    }
    return /** @type {[string, Float32Array]} */ ([name, out]);
  });
  return { base, deltas };
}

/**
 * @typedef {{normalizer: import('./body-types').Object3D, figure: Figure, swap: (source: import('./body-types').Material) => import('./body-types').Material, found:
 * Set<string>}} MeshContext
 */

/** Gives one mesh the skin material and records its morph bindings and silhouette. @param {import('./body-types').Mesh} node @param {MeshContext} context */
function prepareMesh(node, { normalizer, figure, swap, found }) {
  node.castShadow = true;
  node.receiveShadow = true;
  node.material = Array.isArray(node.material) ? node.material.map(swap) : swap(node.material);
  const indices = morphIndices(node);
  indices.forEach(([name]) => found.add(name));
  if (indices.length) figure.bindings.push({ mesh: node, indices });
  figure.shape.meshes.push(silhouetteMesh(node, indices, normalizer));
}

/**
 * Prepares `model` in place. `figure` receives the viewer's bookkeeping: bindings, reliefMaterials,
 * skinMaterials and shape.meshes. Returns the normal relief read from the model's metadata.
 * @param {import('./body-types').Object3D} model the loaded glTF scene
 * @param {{normalizer: import('./body-types').Object3D, contacts: import('./body-types').Decal[], skinUniforms: import('./body-types').SkinUniforms, figure: Figure}} parts
 */
export function prepareModel(model, { normalizer, contacts, skinUniforms, figure }) {
  fitToContract(model, normalizer);
  /** @type {Set<string>} */
  const found = new Set();
  const converted = new Map();
  /** @type {import('./body-types').Color | null} a three.js Color, once the metadata names one */
  let skinTone = null;
  let relief = DEFAULT_NORMAL_RELIEF;
  /** @param {import('./body-types').Material} source */
  const swap = source => {
    if (!converted.has(source)) converted.set(source, convertMaterial(source, { skinUniforms, figure, skinTone }));
    return converted.get(source);
  };
  model.traverse((/** @type {import('./body-types').ModelNode} */ node) => {
    if (node.userData?.healthos) {
      const metadata = readMetadata(node.userData.healthos);
      if (relief === DEFAULT_NORMAL_RELIEF) relief = metadata.relief;
      skinTone = metadata.skinTone ?? skinTone;
    }
    if (node.isMesh) prepareMesh(node, { normalizer, figure, swap, found });
  });
  placeContacts(contacts, figure.shape.meshes);
  return { relief, found };
}
