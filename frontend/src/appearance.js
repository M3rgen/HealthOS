// Appearance: logo, Umay portrait and page background, chosen from presets
// (appearance-presets.js) or uploaded by the user (appearance-upload.js), stored by
// appearance-store.js and applied at runtime without a reload (appearance-apply.js).
// Any failure falls back to the built-in defaults.
import { setupDialog } from './accessibility.js';
import { createAppearanceApplier } from './appearance-apply.js';
import { createAppearanceDialog } from './appearance-dialog.js';
import { FALLBACK, KINDS, UPLOAD, loadManifest, normalize, resolveEntry } from './appearance-presets.js';
import { cleanSelection, createSyncedAppearanceStore } from './appearance-store.js';
import { $, $$ } from './dom.js';
import { t } from './i18n/index.js';

/** @typedef {import('./appearance-presets.js').Kind} Kind */
const TEXT = t.appearance;

/**
 * Wires the dialog's static controls: every kind's texts, and the buttons around the grid.
 * @param {HTMLDialogElement} dialog
 * @param {{reset: () => unknown, removeUpload: (kind: Kind) => unknown, open: () => void}} actions
 */
function wireDialog(dialog, { reset, removeUpload, open }) {
  $$('[data-appearance-kind]', dialog).forEach(group => {
    const kind = /** @type {Kind} */ (group.dataset.appearanceKind);
    const part = (/** @type {string} */ selector) => /** @type {HTMLElement} */ (group.querySelector(selector));
    part('legend').textContent = TEXT.kinds[kind].legend;
    part('.appearance-hint').textContent = TEXT.kinds[kind].hint;
    part('.appearance-hint').id = `appearance-${kind}-hint`;
    part('.appearance-error').id = `appearance-${kind}-error`;
    part('[data-remove]').addEventListener('click', () => removeUpload(kind));
  });
  $('appearance-open')?.addEventListener('click', open);
  $('appearance-close')?.addEventListener('click', () => dialog.close());
  $('appearance-done')?.addEventListener('click', () => dialog.close());
  $('appearance-reset')?.addEventListener('click', reset);
}

/**
 * The saved state and what follows from it. `selection` is what is shown; `saved` is what is
 * stored and shared: an `upload` choice made on a device whose image this browser lacks stays
 * in `saved`, not replaced by the fallback.
 */
function createAppearanceChoices() {
  /** @type {Record<string, import('./ui-types').UploadRecord>} */
  const uploads = {};
  /** @type {Record<string, string>} */
  const blank = {};
  const choices = { manifest: normalize(FALLBACK), selection: blank, uploads, current: { ...blank }, saved: blank };
  return {
    get: () => choices,
    /** @param {Kind} kind */
    resolveId(kind) {
      const id = choices.saved[kind];
      const valid = id === UPLOAD ? Boolean(uploads[kind]) : choices.manifest[kind].some(entry => entry.id === id);
      return valid ? id : choices.manifest[kind][0].id;
    },
    /** @param {Kind} kind @param {string} id @returns {import('./appearance-apply.js').Entry} */
    entryFor(kind, id) {
      if (id === UPLOAD && uploads[kind]) return { id: UPLOAD, label: TEXT.uploaded, upload: uploads[kind] };
      return resolveEntry(kind, choices.manifest[kind].find(entry => entry.id === id) ?? choices.manifest[kind][0]);
    },
  };
}

/**
 * Reads the manifest and this browser's saved choices, then the shared copy from the server
 * unless the user already changed something.
 * @param {object} parts
 * @param {ReturnType<typeof createSyncedAppearanceStore>} parts.store
 * @param {ReturnType<ReturnType<typeof createAppearanceChoices>['get']>} parts.state
 * @param {ReturnType<typeof createAppearanceDialog> | null} parts.form
 * @param {() => Record<string, string>} parts.resolveSelection
 * @param {() => void} parts.applyAll
 * @param {() => number} parts.editCount
 */
function loadAppearance({ store, state, form, resolveSelection, applyAll, editCount }) {
  const root = document.documentElement;
  const ready = (async () => {
    const [manifest, remembered] = await Promise.all([loadManifest(), store.getSelection().catch(() => ({}))]);
    state.manifest = manifest;
    state.saved = cleanSelection(remembered);
    const stored = await Promise.all(KINDS.map(kind => store.getUpload(kind).catch(() => null)));
    KINDS.forEach((kind, i) => { const record = stored[i]; if (record?.files && record.kind === kind) state.uploads[kind] = record; });
    state.selection = resolveSelection();
    KINDS.forEach(kind => form?.renderGroup(kind));
    applyAll();
    root.dataset.appearanceReady = '';
  })().catch(() => { root.dataset.appearanceReady = 'fallback'; });
  // Then the shared copy from the server, unless the user already changed something.
  const synced = ready.then(async () => {
    const before = editCount();
    const remote = store.sync ? await store.sync().catch(() => null) : null;
    if (remote && editCount() === before && JSON.stringify(remote) !== JSON.stringify(state.saved)) {
      state.saved = remote; state.selection = resolveSelection(); applyAll();
    }
    root.dataset.appearanceSynced = remote ? '' : 'local';
  });
  return { ready, synced };
}

export function setupAppearance({ store = createSyncedAppearanceStore() } = {}) {
  const dialog = $('appearance-dialog');
  const modal = dialog && setupDialog(dialog);
  const applyEntry = createAppearanceApplier();
  const choices = createAppearanceChoices();
  const state = choices.get();
  let edits = 0;

  const save = (/** @type {Record<string, string>} */ changes) => { edits += 1; state.saved = { ...state.saved, ...changes }; return store.setSelection(state.saved); };
  const resolveSelection = () => Object.fromEntries(KINDS.map(kind => [kind, choices.resolveId(kind)]));
  const defaults = () => Object.fromEntries(KINDS.map(kind => [kind, state.manifest[kind][0].id]));

  /** @param {Kind} kind */
  function applyKind(kind) {
    const entry = choices.entryFor(kind, state.selection[kind]);
    state.current[kind] = entry.id;
    applyEntry(kind, entry);
  }
  function applyAll() {
    for (const kind of KINDS) {
      try { applyKind(kind); }
      catch { state.selection[kind] = state.manifest[kind][0].id; try { applyKind(kind); } catch { /* Static defaults in index.html/CSS remain. */ } }
    }
    form?.updateForm();
  }

  /** @param {Kind} kind @param {string} id */
  async function choose(kind, id) {
    form?.showError(kind);
    state.selection = { ...state.selection, [kind]: id };
    applyKind(kind); form?.updateForm();
    await save({ [kind]: id });
  }
  /** @param {Kind} kind @param {File} file */
  async function upload(kind, file) {
    const group = /** @type {HTMLElement} */ (dialog.querySelector(`[data-appearance-kind="${kind}"]`));
    form?.showError(kind); group.setAttribute('aria-busy', 'true');
    try {
      const { prepareUpload } = await import('./appearance-upload.js');
      const record = await prepareUpload(kind, file);
      await store.putUpload(kind, record);
      state.uploads[kind] = record;
      form?.renderGroup(kind);
      await choose(kind, UPLOAD);
    } catch (/** @type {unknown} */ problem) {
      form?.showError(kind, (/** @type {{message?: string} | null} */ (problem))?.message || TEXT.errors.failed);
    } finally { group.removeAttribute('aria-busy'); }
  }
  /** @param {Kind} kind */
  async function removeUpload(kind) {
    await store.deleteUpload(kind);
    delete state.uploads[kind];
    if (state.selection[kind] === UPLOAD) { state.selection = { ...state.selection, [kind]: state.manifest[kind][0].id }; await save({ [kind]: state.selection[kind] }); }
    applyKind(kind); form?.renderGroup(kind); form?.updateForm();
  }
  async function reset() {
    state.selection = defaults();
    KINDS.forEach(kind => form?.showError(kind));
    applyAll();
    await save(state.selection);
  }

  const form = dialog && createAppearanceDialog({ dialog, view: choices.get, entryFor: choices.entryFor, onChoose: choose, onUpload: upload });
  if (dialog) wireDialog(dialog, { reset, removeUpload, open: () => modal.open({ initial: dialog.querySelector('input:checked') }) });

  const { ready, synced } = loadAppearance({ store, state, form, resolveSelection, applyAll, editCount: () => edits });
  return { ready, synced, choose, reset };
}
