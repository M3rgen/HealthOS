// Puts a chosen logo, portrait or background on the page: images, tab icons and CSS variables.
// Uploaded images are shown through blob: URLs only, released once their replacement has loaded.
import { DEFAULT_GLYPH, FALLBACK } from './appearance-presets.js';
import { $$ } from './dom.js';
import { iconMarkup } from './icons.js';

/** @typedef {import('./appearance-presets.js').Kind} Kind */
/**
 * @typedef {{id: string, label?: string, files: Record<string, string>, upload?: undefined} | {id: string, label?: string, files?: undefined, upload:
 * import('./ui-types').UploadRecord}} Entry
 */

/** @param {string | null | undefined} url */
export const cssUrl = url => (url ? `url("${url}")` : 'none');

/** @param {{files: Record<string, unknown>}} record @returns {Record<string, string>} */
export function blobUrls(record) {
  /** @type {Record<string, string>} */
  const urls = {};
  for (const [name, blob] of Object.entries(record.files)) if (blob instanceof Blob) urls[name] = URL.createObjectURL(blob);
  return urls;
}

/** @param {HTMLElement} container @param {{files: Record<string, string>}} entry @param {Element | null} [inline] */
export function renderMark(container, entry, inline) {
  container.replaceChildren();
  container.className = 'appearance-mark';
  if (inline) { container.append(inline.cloneNode(true)); return; }
  // Full-colour marks render as designed (two-tone accents); a mono-only mark is tinted amber.
  if (entry.files.mark) {
    const image = new Image();
    image.alt = ''; image.decoding = 'async'; image.src = entry.files.mark;
    container.append(image);
  } else {
    container.classList.add('is-mask');
    container.style.setProperty('--mark', cssUrl(entry.files.markMono));
  }
}

/** @param {string} id @param {string} rel @param {string | null | undefined} href @param {string | null} [type] @param {string | null} [sizes] */
function setLink(id, rel, href, type, sizes) {
  const old = document.getElementById(id);
  if (!href) { old?.remove(); return; }
  // Replacing the element (not just href) makes browsers refresh the tab icon.
  const link = document.createElement('link');
  link.id = id; link.rel = rel; link.href = href;
  if (type) link.type = type;
  if (sizes) link.setAttribute('sizes', sizes);
  if (old) old.replaceWith(link); else document.head.append(link);
}

/** @param {Record<string, string>} files @param {{type: string}} upload */
function linkUploadedIcons(files, upload) {
  const svg = upload.type === 'image/svg+xml';
  setLink('favicon-primary', 'icon', svg ? files.mark : files.favicon32, svg ? 'image/svg+xml' : 'image/png', svg ? null : '32x32');
  setLink('favicon-fallback', 'icon', files.favicon32, 'image/png', '32x32');
  setLink('favicon-small', 'icon', files.favicon16, 'image/png', '16x16');
  setLink('apple-touch', 'apple-touch-icon', files.appleTouch);
}

// Presets with an SVG favicon use it; presets without one (e.g. the yurt, whose
// pixel-snapped PNGs beat its detailed SVG at 16px) lead with the 32px PNG.
/** @param {Record<string, string>} f */
function linkPresetIcons(f) {
  const primary = f.favicon || f.favicon32 || f.mark;
  setLink('favicon-primary', 'icon', primary, iconType(primary), !f.favicon && f.favicon32 ? '32x32' : null);
  setLink('favicon-small', 'icon', f.favicon16 || null, 'image/png', '16x16');
  linkFallbackIcon(f);
  setLink('apple-touch', 'apple-touch-icon', f.appleTouch || FALLBACK.logo[0].files.appleTouch);
}

/** @param {string} path */
function iconType(path) {
  if (/\.svg$/i.test(path)) return 'image/svg+xml';
  return /\.png$/i.test(path) ? 'image/png' : null;
}

/** @param {Record<string, string>} f */
function linkFallbackIcon(f) {
  if (f.faviconIco) setLink('favicon-fallback', 'icon', f.faviconIco, null, 'any');
  else setLink('favicon-fallback', 'icon', f.favicon ? f.favicon32 : null, 'image/png', '32x32');
}

/** @param {Record<string, string>} files */
function applyUmay(files) {
  /** @type {HTMLImageElement[]} */ ($$('.assistant-avatar, [data-appearance-preview="avatar"]')).forEach(image => {
    image.src = files.src64; image.srcset = `${files.src64} 1x, ${files.src64x2} 2x`;
  });
  /** @type {HTMLImageElement[]} */ ($$('.assistant-intro img')).forEach(image => {
    image.src = files.src256; image.srcset = `${files.src256} 1x, ${files.src512} 2x`;
  });
  const glyph = files.glyph && files.glyph !== DEFAULT_GLYPH ? files.glyph : null;
  $$('#assistant-open > [data-icon], [data-appearance-preview="glyph"]').forEach(slot => {
    slot.classList.toggle('glyph-mask', Boolean(glyph));
    if (glyph) { slot.replaceChildren(); slot.style.setProperty('--glyph', cssUrl(glyph)); }
    else { slot.style.removeProperty('--glyph'); slot.innerHTML = iconMarkup('umay'); }
  });
}

/** @param {Record<string, string>} files @param {{mode: string} | null} upload */
function applyBackground(files, upload) {
  /** @type {(name: string, value: string) => void} */
  const set = (name, value) => document.documentElement.style.setProperty(name, value);
  if (upload) {
    const tile = upload.mode === 'tile';
    set('--bg-tile', cssUrl(files.image)); set('--bg-repeat', tile ? 'repeat' : 'no-repeat');
    set('--bg-size', tile ? 'auto' : 'cover'); set('--bg-position', tile ? '0 0' : 'center');
    set('--bg-vignette', cssUrl(FALLBACK.background[0].files.vignette));
    set('--bg-dim', tile ? 'rgb(24 24 20 / .6)' : 'rgb(24 24 20 / .72)');
  } else {
    set('--bg-tile', cssUrl(files.tile)); set('--bg-repeat', 'repeat'); set('--bg-size', 'auto'); set('--bg-position', '0 0');
    set('--bg-vignette', cssUrl(files.vignette)); set('--bg-dim', 'transparent');
  }
}

export function createAppearanceApplier() {
  const inlineMark = /** @type {HTMLElement | null} */ (document.querySelector('.topbar .brand')?.querySelector('.brand-mark') ?? null);
  const headerMark = document.createElement('span');
  headerMark.hidden = true;
  inlineMark?.after(headerMark);
  /** @type {Record<Kind, string[]>} */
  const live = { logo: [], umay: [], background: [] }; // blob URLs currently shown

  /** @param {Entry} entry @param {Record<string, string>} files @param {import('./ui-types').UploadRecord | null} upload */
  function applyLogo(entry, files, upload) {
    // index.html paints the default preset's mark inline (data-preset) for first paint.
    const isDefault = !upload && Boolean(inlineMark) && entry.id === inlineMark?.dataset.preset && entry.files?.mark === FALLBACK.logo[0].files.mark;
    inlineMark?.toggleAttribute('hidden', !isDefault); // SVGElement has no .hidden property
    headerMark.hidden = isDefault;
    const shown = /** @type {{files: Record<string, string>}} */ (upload ? { files: { mark: files.mark } } : entry);
    if (!isDefault) { renderMark(headerMark, shown); headerMark.classList.add('brand-mark-custom'); }
    $$('[data-appearance-preview="logo"]').forEach(el => renderMark(el, shown, isDefault ? inlineMark : null));
    if (upload) linkUploadedIcons(files, upload);
    else linkPresetIcons(files);
  }

  /** @param {Kind} kind @param {Entry} entry */
  return function apply(kind, entry) {
    const previous = live[kind];
    const upload = entry.upload ?? null;
    const files = upload ? blobUrls(upload) : /** @type {Record<string, string>} */ (entry.files);
    live[kind] = upload ? Object.values(files) : [];
    if (kind === 'logo') applyLogo(entry, files, upload);
    else if (kind === 'umay') applyUmay(files);
    else applyBackground(files, upload);
    // Release the previous blob URLs once the new images have had a chance to load.
    if (previous.length) setTimeout(() => previous.forEach(url => URL.revokeObjectURL(url)), 1500);
  };
}
