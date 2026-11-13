// Validates an uploaded image and derives the sizes the interface needs. Uploaded files are only
// ever shown through <img src> or CSS url() with a blob: URL; SVG text is never parsed into the DOM.
import { t } from './i18n/index.js';

const MAX_BYTES = 2 * 1024 * 1024;
/** @type {Record<string, string>} */
const TYPES = { 'image/png': 'png', 'image/jpeg': 'jpg', 'image/webp': 'webp', 'image/svg+xml': 'svg' };
/** @type {Record<string, number>} */
const MIN_SIDE = { logo: 16, umay: 64, background: 64 };
const MAX_SIDE = 8192;
const ERRORS = t.appearance.errors;

/** @param {Blob} blob @returns {Promise<{image: HTMLImageElement, url: string}>} */
function decode(blob) {
  const url = URL.createObjectURL(blob);
  const image = new Image();
  image.decoding = 'async';
  image.src = url;
  const timeout = new Promise((_, reject) => setTimeout(() => reject(new Error('timeout')), 8000));
  return Promise.race([image.decode(), timeout]).then(() => ({ image, url }), error => { URL.revokeObjectURL(url); throw error; });
}

/** @param {HTMLCanvasElement} canvas @param {string} [type] @param {number} [quality] @returns {Promise<Blob>} */
function canvasBlob(canvas, type = 'image/png', quality) {
  return new Promise((resolve, reject) => {
    try { canvas.toBlob(blob => blob ? resolve(blob) : reject(new Error('encode')), type, quality); } catch (error) { reject(error); }
  });
}

/** @param {number} width @param {number} height */
function drawingCanvas(width, height) {
  const canvas = document.createElement('canvas');
  canvas.width = width;
  canvas.height = height;
  const context = /** @type {CanvasRenderingContext2D} */ (canvas.getContext('2d'));
  context.imageSmoothingQuality = 'high';
  return { canvas, context };
}

// Square render with stepwise halving so small sizes stay sharp, not aliased.
/** @param {HTMLImageElement} image @param {number} size @param {'cover'|'contain'} fit @param {string} [background] */
async function square(image, size, fit, background) {
  const width = image.naturalWidth || 512, height = image.naturalHeight || 512;
  /** @type {CanvasImageSource} */
  let source = image;
  let sw = width, sh = height, sx = 0, sy = 0;
  if (fit === 'cover') { const side = Math.min(width, height); sx = (width - side) / 2; sy = (height - side) / 2; sw = sh = side; }
  while (Math.max(sw, sh) > size * 2) {
    const step = drawingCanvas(Math.max(1, Math.round(sw / 2)), Math.max(1, Math.round(sh / 2)));
    step.context.drawImage(source, sx, sy, sw, sh, 0, 0, step.canvas.width, step.canvas.height);
    source = step.canvas; sx = sy = 0; sw = step.canvas.width; sh = step.canvas.height;
  }
  const { canvas, context } = drawingCanvas(size, size);
  if (background) { context.fillStyle = background; context.fillRect(0, 0, size, size); }
  const scale = fit === 'cover' ? size / Math.max(sw, sh) : Math.min(size / sw, size / sh) * (background ? .78 : 1);
  const dw = sw * scale, dh = sh * scale;
  context.drawImage(source, sx, sy, sw, sh, (size - dw) / 2, (size - dh) / 2, dw, dh);
  return canvasBlob(canvas);
}

/** @param {File} file */
function fileType(file) {
  if (TYPES[file.type]) return file.type;
  const extension = /\.([a-z0-9]+)$/i.exec(file.name || '')?.[1]?.toLowerCase();
  return Object.keys(TYPES).find(type => TYPES[type] === extension || (extension === 'jpeg' && type === 'image/jpeg')) ?? null;
}

/** @param {File | null | undefined} file @returns {Promise<string>} the accepted content type */
async function validatedType(file) {
  const type = file && fileType(file);
  if (!file || !type) throw new Error(ERRORS.type);
  if (!file.size) throw new Error(ERRORS.empty);
  if (file.size > MAX_BYTES) throw new Error(ERRORS.size);
  // Only a sanity check; the markup itself is never inserted into the page.
  if (type === 'image/svg+xml' && !/<svg[\s>]/i.test(await file.slice(0, 4096).text())) throw new Error(ERRORS.svg);
  return type;
}

/** @param {HTMLImageElement} image @param {string} kind */
function assertDimensions(image, kind) {
  const { naturalWidth: width, naturalHeight: height } = image;
  if (width < MIN_SIDE[kind] || height < MIN_SIDE[kind]) throw new Error(ERRORS.tooSmall(MIN_SIDE[kind]));
  if (width > MAX_SIDE || height > MAX_SIDE) throw new Error(ERRORS.tooLarge(MAX_SIDE));
}

/** @param {string} kind @param {Blob} original @param {(size: number, fit: 'cover'|'contain', background?: string) => Promise<Blob>} derive */
async function deriveFiles(kind, original, derive) {
  if (kind === 'logo') {
    const [favicon32, favicon16, appleTouch] = await Promise.all([derive(32, 'contain'), derive(16, 'contain'), derive(180, 'contain', '#181814')]);
    return { mark: original, favicon32, favicon16, appleTouch };
  }
  if (kind === 'umay') {
    const [src512, src256, src64, src64x2] = await Promise.all([derive(512, 'cover'), derive(256, 'cover'), derive(64, 'cover'), derive(128, 'cover')]);
    return { src512, src256, src64, src64x2 };
  }
  return { image: original };
}

/** @param {string} kind @param {File | null | undefined} file */
export async function prepareUpload(kind, file) {
  const type = await validatedType(file);
  const accepted = /** @type {File} */ (file);
  const vector = type === 'image/svg+xml';
  const original = accepted.slice(0, accepted.size, type);
  let decoded;
  try { decoded = await decode(original); } catch { throw new Error(ERRORS.decode); }
  const { image, url } = decoded;
  try {
    if (!vector) assertDimensions(image, kind);
    // Canvas output can fail (e.g. an SVG the browser refuses to rasterize); keep the original then.
    /** @type {(size: number, fit: 'cover'|'contain', background?: string) => Promise<Blob>} */
    const derive = (size, fit, background) => square(image, size, fit, background).catch(() => original);
    const files = await deriveFiles(kind, original, derive);
    const { naturalWidth: width, naturalHeight: height } = image;
    const mode = kind === 'background' && !vector && Math.max(width, height) < 512 ? 'tile' : 'cover';
    return { kind, name: String(accepted.name || '').slice(0, 80), type, width, height, mode, files, createdAt: Date.now() };
  } finally { URL.revokeObjectURL(url); }
}
