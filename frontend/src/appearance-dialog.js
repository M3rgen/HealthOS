// The Appearance dialog: one group of preset tiles and an upload tile per kind.
import { replaceWithFocus } from './accessibility.js';
import { blobUrls, cssUrl, renderMark } from './appearance-apply.js';
import { FALLBACK, KINDS, UPLOAD, resolveEntry } from './appearance-presets.js';
import { $, $$ } from './dom.js';
import { iconMarkup } from './icons.js';
import { t } from './i18n/index.js';

/** @typedef {import('./appearance-presets.js').Kind} Kind */
const TEXT = t.appearance;

/** @param {HTMLElement} box @param {import('./appearance-apply.js').Entry} entry @param {Record<string, string> | null} urls */
function fillLogoThumb(box, entry, urls) {
  const mark = document.createElement('span');
  renderMark(mark, urls ? { files: { mark: urls.mark } } : /** @type {{files: Record<string, string>}} */ (entry));
  box.append(mark);
}

/** @param {HTMLElement} box @param {import('./appearance-apply.js').Entry} entry @param {Record<string, string> | null} urls */
function fillUmayThumb(box, entry, urls) {
  const image = new Image();
  image.alt = ''; image.decoding = 'async'; image.loading = 'lazy';
  image.src = urls ? urls.src256 : (/** @type {Record<string, string>} */ (entry.files)).src256;
  box.append(image);
}

/** @param {HTMLElement} box @param {import('./appearance-apply.js').Entry} entry @param {Record<string, string> | null} urls */
function fillBackgroundThumb(box, entry, urls) {
  const upload = entry.upload;
  box.style.setProperty('--thumb-tile', cssUrl(urls ? urls.image : (/** @type {Record<string, string>} */ (entry.files)).tile));
  box.style.setProperty('--thumb-size', upload && upload.mode !== 'tile' ? 'cover' : 'auto');
  box.style.setProperty('--thumb-vignette', cssUrl(upload ? FALLBACK.background[0].files.vignette : (/** @type {Record<string, string>} */ (entry.files)).vignette));
}

/** @param {Kind} kind @param {import('./appearance-apply.js').Entry} entry */
function thumb(kind, entry) {
  const box = document.createElement('span');
  box.className = `preset-thumb thumb-${kind}`;
  const urls = entry.upload ? blobUrls(entry.upload) : null;
  if (urls) box.dataset.blobs = Object.values(urls).join(' ');
  if (kind === 'logo') fillLogoThumb(box, entry, urls);
  else if (kind === 'umay') fillUmayThumb(box, entry, urls);
  else fillBackgroundThumb(box, entry, urls);
  return box;
}

/**
 * @param {object} options
 * @param {HTMLDialogElement} options.dialog
 * @param {() => {manifest: import('./appearance-presets.js').Manifest, uploads: Record<string, import('./ui-types').UploadRecord>, current: Record<string, string>}} options.view
 * @param {(kind: Kind, id: string) => import('./appearance-apply.js').Entry} options.entryFor
 * @param {(kind: Kind, id: string) => unknown} options.onChoose
 * @param {(kind: Kind, file: File) => unknown} options.onUpload
 */
export function createAppearanceDialog({ dialog, view, entryFor, onChoose, onUpload }) {
  /** @param {Kind} kind @param {import('./appearance-apply.js').Entry} entry */
  function tile(kind, entry) {
    const label = document.createElement('label');
    label.className = 'preset-tile';
    const input = document.createElement('input');
    input.type = 'radio'; input.name = `appearance-${kind}`; input.value = entry.id; input.className = 'tile-input';
    input.checked = entry.id === view().current[kind];
    input.addEventListener('change', () => { if (input.checked) onChoose(kind, entry.id); });
    const caption = document.createElement('span');
    caption.className = 'preset-label';
    caption.textContent = /** @type {string} */ (entry.label);
    label.title = entry.upload ? entry.upload.name : /** @type {string} */ (entry.label);
    label.append(input, thumb(kind, entry), caption);
    return label;
  }

  /** @param {Kind} kind */
  function uploadTile(kind) {
    const label = document.createElement('label');
    label.className = 'preset-tile upload-tile';
    const input = document.createElement('input');
    input.type = 'file'; input.className = 'tile-input'; input.dataset.upload = kind;
    input.accept = 'image/png,image/jpeg,image/webp,image/svg+xml,.png,.jpg,.jpeg,.webp,.svg';
    input.setAttribute('aria-describedby', `appearance-${kind}-hint appearance-${kind}-error`);
    input.addEventListener('change', () => { const file = input.files?.[0]; input.value = ''; if (file) onUpload(kind, file); });
    const box = document.createElement('span');
    box.className = 'preset-thumb thumb-upload';
    box.innerHTML = iconMarkup('plus');
    const caption = document.createElement('span');
    caption.className = 'preset-label';
    caption.textContent = view().uploads[kind] ? TEXT.replace : TEXT.upload;
    label.append(input, box, caption);
    return label;
  }

  /** @param {Kind} kind */
  function renderGroup(kind) {
    const group = dialog.querySelector(`[data-appearance-kind="${kind}"]`);
    const grid = group?.querySelector('.preset-grid');
    if (!group || !grid) return;
    const { manifest, uploads } = view();
    replaceWithFocus(group, () => {
      $$('[data-blobs]', grid).forEach(el => (el.dataset.blobs ?? '').split(' ').forEach((/** @type {string} */ url) => URL.revokeObjectURL(url)));
      const entries = /** @type {import('./appearance-apply.js').Entry[]} */ (manifest[kind].map(entry => resolveEntry(kind, entry)));
      if (uploads[kind]) entries.push(entryFor(kind, UPLOAD));
      grid.replaceChildren(...entries.map(entry => tile(kind, entry)), uploadTile(kind));
      /** @type {HTMLElement} */ (group.querySelector('[data-remove]')).hidden = !uploads[kind];
    }, old => old.matches('[data-upload]') ? group.querySelector('[data-upload]') :
      group.querySelector(`input[value="${CSS.escape((/** @type {HTMLInputElement} */ (old)).value ?? '')}"]`) || group.querySelector('input:checked'), $('appearance-close'));
  }

  function updateForm() {
    const { current } = view();
    for (const kind of KINDS) {
      /** @type {HTMLInputElement[]} */ ($$(`input[name="appearance-${kind}"]`, dialog)).forEach(input => { input.checked = input.value === current[kind]; });
    }
  }

  /** @param {Kind} kind @param {string} [message] */
  function showError(kind, message = '') {
    const node = /** @type {HTMLElement | null} */ (dialog.querySelector(`[data-appearance-kind="${kind}"] .appearance-error`));
    if (node) { node.textContent = message; node.hidden = !message; }
    dialog.querySelector(`[data-upload="${kind}"]`)?.setAttribute('aria-invalid', String(Boolean(message)));
  }

  return { renderGroup, updateForm, showError };
}
