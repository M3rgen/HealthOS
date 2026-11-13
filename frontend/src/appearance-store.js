// Where the appearance choices are kept. Store adapter API:
//   getSelection() → {logo?, umay?, background?}   setSelection(selection)
//   getUpload(kind) → record|null   putUpload(kind, record)   deleteUpload(kind)
// createSyncedAppearanceStore (the default) keeps the preset ids on the server
// (/api/settings/appearance) so every device shows the same choice, with
// localStorage as the offline copy. Uploaded images are never sent anywhere: they
// stay in this browser's IndexedDB; a device without the upload shows the default.
import { request } from './api.js';
import { KINDS, PRESET_ID } from './appearance-presets.js';
import { migrateLegacyKeys, migrateLegacyUploads } from './legacy-names.js';

const REMOTE_URL = '/api/settings/appearance';

/** @param {Record<string, unknown> | null | undefined} value @returns {Record<string, string>} */
export const cleanSelection = value => Object.fromEntries(KINDS.flatMap(kind => (
  typeof value?.[kind] === 'string' && PRESET_ID.test(value[kind]) ? [[kind, value[kind]]] : [])));

/** @param {string} dbName @returns {Promise<IDBDatabase | null>} */
function openDatabase(dbName) {
  return new Promise((/** @type {(db: IDBDatabase | null) => void} */ resolve) => {
    try {
      const open = indexedDB.open(dbName, 1);
      open.onupgradeneeded = () => open.result.createObjectStore('uploads');
      open.onsuccess = () => resolve(open.result);
      open.onerror = open.onblocked = () => resolve(null);
    } catch { resolve(null); }
  }).then(async db => {
    // Uploads kept under the old database name are copied in once; the old one stays.
    if (db) await migrateLegacyUploads(db);
    return db;
  });
}

// localStorage for ids, IndexedDB for blobs, memory last.
export function createLocalAppearanceStore({ key = 'healthos.appearance', dbName = 'healthos-appearance' } = {}) {
  migrateLegacyKeys();
  /** @type {Map<string, import('./ui-types').UploadRecord>} */
  const memory = new Map();
  /** @type {Promise<IDBDatabase | null> | undefined} */
  let database;
  /**
   * @template T
   * @param {IDBTransactionMode} mode
   * @param {(store: IDBObjectStore) => IDBRequest} action
   * @param {() => T} fallback
   * @returns {Promise<T>}
   */
  const run = async (mode, action, fallback) => {
    const db = await (database ??= openDatabase(dbName));
    if (!db) return fallback();
    return new Promise(resolve => {
      try {
        const transaction = db.transaction('uploads', mode);
        const result = action(transaction.objectStore('uploads'));
        transaction.oncomplete = () => resolve(/** @type {T} */ (result.result));
        transaction.onerror = transaction.onabort = () => resolve(fallback());
      } catch { resolve(fallback()); }
    });
  };
  return {
    async getSelection() {
      try { const value = JSON.parse(localStorage.getItem(key) || '{}'); return value && typeof value === 'object' ? value : {}; }
      catch { return {}; }
    },
    /** @param {Record<string, string>} selection */
    async setSelection(selection) {
      try { localStorage.setItem(key, JSON.stringify(selection)); } catch { /* Private mode: keep for this session only. */ }
    },
    /** @param {string} kind */
    async getUpload(kind) {
      return (await run('readonly', store => store.get(kind), () => memory.get(kind))) ?? memory.get(kind) ?? null;
    },
    /** @param {string} kind @param {import('./ui-types').UploadRecord} record */
    async putUpload(kind, record) {
      memory.set(kind, record);
      await run('readwrite', store => store.put(record, kind), () => undefined);
    },
    /** @param {string} kind */
    async deleteUpload(kind) {
      memory.delete(kind);
      await run('readwrite', store => store.delete(kind), () => undefined);
    },
  };
}

// Shared selection. getSelection() answers from localStorage at once (first paint);
// sync() then asks the server, whose copy wins when reachable. Offline, the local
// copy stays in use and a write that could not reach the server is marked pending,
// then sent on the next load that reaches it (last write wins).
export function createSyncedAppearanceStore({ local = createLocalAppearanceStore(), url = REMOTE_URL, pendingKey = 'healthos.appearance.pending' } = {}) {
  migrateLegacyKeys();
  /** @param {boolean} [value] */
  const pending = value => {
    try {
      if (value === undefined) return localStorage.getItem(pendingKey) === '1';
      if (value) localStorage.setItem(pendingKey, '1'); else localStorage.removeItem(pendingKey);
    } catch { /* Private mode: nothing to resume later. */ }
    return false;
  };
  /** @type {Record<string, string> | null} */
  let latest = null;
  /** @type {Promise<unknown>} */
  let queue = Promise.resolve();
  /** @param {Record<string, string>} selection */
  const enqueue = selection => {
    latest = selection; pending(true);
    // One request at a time, newest last; superseded writes are skipped.
    queue = queue.then(() => (latest === selection ? push(selection) : undefined));
    return queue;
  };
  /** @param {Record<string, string>} selection */
  const push = async selection => {
    try {
      await request(url, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json', 'X-HealthOS-Request': '1' },
        body: JSON.stringify(cleanSelection(selection)),
      });
      if (latest === selection) pending(false);
    } catch { /* Offline or rejected: stays pending; the local copy still applies. */ }
  };
  return {
    ...local,
    async getSelection() { return cleanSelection(await local.getSelection()); },
    // → the server's selection, or null when it is unreachable or this browser's
    // copy was sent instead (an unsent change, or a server with nothing stored yet).
    async sync() {
      const cached = cleanSelection(await local.getSelection());
      /** @type {import('./api-types').StoredAppearance} */
      let remote;
      try { remote = await request(url); } catch { return null; }
      if (pending() || (!remote?.updated_at && Object.keys(cached).length)) { await enqueue(cached); return null; }
      const selection = cleanSelection(remote?.selection);
      await local.setSelection(selection);
      return selection;
    },
    /** @param {Record<string, string>} selection */
    async setSelection(selection) {
      await local.setSelection(selection);
      return enqueue(selection);
    },
  };
}
