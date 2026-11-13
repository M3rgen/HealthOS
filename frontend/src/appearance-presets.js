// The built-in appearance choices: read from public/assets/presets/manifest.json, falling back to
// the defaults below when it cannot be loaded or lists nothing usable.
import { language, t } from './i18n/index.js';

export const KINDS = /** @type {const} */ (['logo', 'umay', 'background']);
/** @typedef {(typeof KINDS)[number]} Kind */
/** @typedef {{id: string, label: string, builtin: boolean, files: Record<string, string>}} Preset */
/** @typedef {Record<Kind, Preset[]>} Manifest */

const MANIFEST_URL = '/assets/presets/manifest.json';
export const UPLOAD = 'upload';
export const PRESET_ID = /^[a-z0-9][a-z0-9-]{0,47}$/;
export const DEFAULT_GLYPH = '/assets/umay/umay-glyph.svg';
const TEXT = t.appearance;

// Used when the manifest cannot be loaded or lists nothing usable.
/** @type {Manifest} */
export const FALLBACK = {
  logo: [{ id: 'yurt', label: TEXT.presets.yurt, builtin: true, files: {
    mark: '/assets/presets/logo/yurt/mark.svg', markMono: '/assets/presets/logo/yurt/mark-mono.svg',
    // No SVG favicon on purpose: browsers prefer it over the pixel-snapped 16/32 PNGs.
    faviconIco: '/assets/presets/logo/yurt/favicon.ico',
    favicon16: '/assets/presets/logo/yurt/favicon-16.png', favicon32: '/assets/presets/logo/yurt/favicon-32.png',
    appleTouch: '/assets/presets/logo/yurt/apple-touch-icon.png' } },
  { id: 'healthos', label: TEXT.presets.healthos, builtin: true, files: {
    mark: '/assets/brand/healthos-mark-amber.svg', markMono: '/assets/brand/healthos-mark.svg',
    favicon: '/assets/brand/favicon.svg', faviconIco: '/assets/brand/favicon.ico', appleTouch: '/assets/brand/apple-touch-icon.png' } }],
  umay: [{ id: 'umay', label: TEXT.presets.umay, builtin: true, files: {
    src512: '/assets/umay/umay.webp', src256: '/assets/umay/umay-256.webp', src64: '/assets/umay/umay-64.webp',
    src64x2: '/assets/umay/umay-64@2x.webp', glyph: DEFAULT_GLYPH } }],
  background: [{ id: 'warm-grain', label: TEXT.presets.warmGrain, builtin: true, files: {
    tile: '/assets/bg/grain-tile.webp', vignette: '/assets/bg/vignette.webp' } }],
};
/** @type {Record<Kind, string[]>} */
const REQUIRED = { logo: ['mark', 'markMono'], umay: ['src512', 'src256'], background: ['tile'] };

/** @param {unknown} value @returns {string | null} a same-origin /assets/ path that is safe inside CSS url() */
function assetPath(value) {
  if (typeof value !== 'string') return null;
  try {
    const url = new URL(value, location.origin);
    return url.origin === location.origin && url.pathname.startsWith('/assets/') && !/["'()\\\s]/.test(url.pathname) ? url.pathname : null;
  } catch { return null; }
}

/** @param {Record<string, unknown>} raw only the file paths that are safe to use */
function safeFiles(raw) {
  /** @type {Record<string, string>} */
  const files = {};
  for (const [name, path] of Object.entries(raw)) {
    const safe = assetPath(path);
    if (safe) files[name] = safe;
  }
  return files;
}

/** The shown name in the interface language, capped to what a tile can hold. @param {import('./ui-types').RawPreset} entry @param {string} id */
const presetLabel = (entry, id) => String((language === 'en' && entry.labelEn) || entry.label || id).slice(0, 40);

/** @param {import('./ui-types').RawPreset} entry @param {Kind} kind @param {Set<string>} seen @returns {Preset | null} */
function normalizeEntry(entry, kind, seen) {
  const id = String(entry?.id ?? '');
  if (!PRESET_ID.test(id) || id === UPLOAD || seen.has(id)) return null;
  const files = safeFiles(entry.files ?? {});
  if (!REQUIRED[kind].some(name => files[name])) return null;
  seen.add(id);
  return { id, label: presetLabel(entry, id), builtin: entry.builtin !== false, files };
}

/** @param {Partial<Record<Kind, import('./ui-types').RawPreset[]>> | null | undefined} raw @returns {Manifest} */
export function normalize(raw) {
  const out = /** @type {Manifest} */ ({});
  for (const kind of KINDS) {
    const seen = new Set();
    const entries = Array.isArray(raw?.[kind]) ? raw[kind] : [];
    const presets = entries.flatMap((/** @type {import('./ui-types').RawPreset} */ entry) => normalizeEntry(entry, kind, seen) ?? []);
    out[kind] = presets.length ? presets : FALLBACK[kind];
  }
  return out;
}

export async function loadManifest() {
  try {
    const response = await fetch(MANIFEST_URL, { cache: 'no-cache', signal: AbortSignal.timeout(8000) });
    if (!response.ok) throw new Error(String(response.status));
    return normalize(await response.json());
  } catch { return normalize(FALLBACK); }
}

/** Fills the optional portrait sizes from the ones the preset has. @template {{files: Record<string, string>}} T @param {Kind} kind @param {T} entry @returns {T} */
export function resolveEntry(kind, entry) {
  if (kind !== 'umay') return entry;
  const f = entry.files;
  const big = f.src512 || f.src256;
  return { ...entry, files: { ...f, src512: big, src256: f.src256 || big, src64: f.src64 || f.src256 || big, src64x2: f.src64x2 || f.src256 || big } };
}
