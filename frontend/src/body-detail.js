// Skin micro-detail: two optional tiling textures. Missing files simply leave the detail off.
import { NoColorSpace, RepeatWrapping, TextureLoader } from 'three';
import { SKIN_DETAIL } from './body-skin.js';

// A tile is about 6 cm of skin. On a figure drawn under this many device pixels tall each tile is
// a few pixels and mip-mapping flattens it to neutral: with and without the textures no pixel of
// the figure differed by more than 2/255 up to 920 device pixels (4/255 at 1326), so the 456 KB
// is skipped there. A figure that later grows past this loads them then.
const MIN_DETAIL_PIXELS = 1000;

/**
 * @param {object} options
 * @param {import('./body-types').Renderer} options.renderer
 * @param {HTMLElement} options.container
 * @param {import('./body-types').SkinUniforms} options.skinUniforms
 * @param {{skinMaterials: import('./body-types').Material[]}} options.parts
 * @param {{disposed: boolean, dirty: boolean}} options.flags
 */
export function createSkinDetail({ renderer, container, skinUniforms, parts, flags }) {
  /** @type {import('./body-types').Texture[]} */
  const textures = [];
  let requested = false;

  /** @param {string} url @param {string} uniform @param {string} define */
  function load(url, uniform, define) {
    new TextureLoader().load(url, (/** @type {import('./body-types').Texture} */ texture) => {
      if (flags.disposed) { texture.dispose(); return; }
      texture.wrapS = texture.wrapT = RepeatWrapping;
      texture.colorSpace = NoColorSpace;
      texture.anisotropy = Math.min(8, renderer.capabilities.getMaxAnisotropy());
      skinUniforms[uniform].value = texture;
      textures.push(texture);
      for (const material of parts.skinMaterials) { material.defines[define] = ''; material.needsUpdate = true; }
      container.dataset.skinDetail = [container.dataset.skinDetail, define].filter(Boolean).join(',');
      flags.dirty = true;
    }, undefined, () => { /* optional asset: render without micro-detail */ });
  }

  return {
    textures,
    /** Requests the textures once the figure is drawn large enough to show them. @param {number} drawnPixels */
    fit(drawnPixels) {
      if (requested || drawnPixels < MIN_DETAIL_PIXELS) return;
      requested = true;
      load(SKIN_DETAIL.normalUrl, 'skinDetailNormal', 'SKIN_DETAIL_NORMAL');
      load(SKIN_DETAIL.roughnessUrl, 'skinDetailRoughness', 'SKIN_DETAIL_ROUGHNESS');
    },
  };
}
