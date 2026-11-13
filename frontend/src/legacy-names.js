// Carries browser data over from the product's earlier, misspelled name ("healtos"). Each
// copy runs once, fills only what the new name does not already hold, and never removes or
// changes the old copy: a browser that downgrades still finds its data where it was.
import { browserStorage } from './local-store.js';

// New localStorage key → the key the same value was kept under before.
export const LEGACY_KEYS = {
  'healthos.timezone.v2': 'healtos.timezone.v2',
  'healthos.motion': 'healtos.motion',
  'healthos.display.pending': 'healtos.display.pending',
  'healthos.appearance': 'healtos.appearance',
  'healthos.appearance.pending': 'healtos.appearance.pending',
};
// Set once the keys were carried over, so a preference the app later removes on purpose
// (e.g. motion back to its default, a pending mark after a send) does not come back.
export const KEYS_MIGRATED = 'healthos.legacy-keys.migrated';

/** @param {Pick<Storage, 'getItem'|'setItem'>|null} [storage] */
export function migrateLegacyKeys(storage) {
  try {
    storage ??= browserStorage();
    if (!storage || storage.getItem(KEYS_MIGRATED) === '1') return;
    for (const [key, legacy] of Object.entries(LEGACY_KEYS)) {
      if (storage.getItem(key) !== null) continue;
      const value = storage.getItem(legacy);
      if (value !== null) storage.setItem(key, value);
    }
    storage.setItem(KEYS_MIGRATED, '1');
  } catch {
    /* Restricted browser storage: nothing to carry over; tried again on the next load. */
  }
}

export const LEGACY_UPLOAD_DB = 'healtos-appearance';
// Kept in the new database's store once its uploads were carried over; like the key marker,
// it keeps an upload the user removed later from coming back. getUpload never reads it.
export const UPLOADS_MIGRATED = 'migrated:healtos-appearance';

/** @template T @param {IDBRequest<T>} request @returns {Promise<T>} */
const settle = request => new Promise((resolve, reject) => {
  request.onsuccess = () => resolve(request.result);
  request.onerror = () => reject(request.error);
});

// → the existing database, null when it never existed, undefined when that is unknown.
/** @param {IDBFactory} factory @param {string} name @returns {Promise<IDBDatabase | null | undefined>} */
function openExisting(factory, name) {
  return new Promise((resolve) => {
    /** @type {IDBOpenDBRequest} */
    let request;
    let absent = false;
    try { request = factory.open(name); } catch { resolve(undefined); return; }
    request.onupgradeneeded = () => {
      absent = true; // Opening created it: abort, so no empty database is left behind.
      try { request.transaction?.abort(); } catch { /* resolved by onerror */ }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = (event) => { event?.preventDefault?.(); resolve(absent ? null : undefined); };
    request.onblocked = () => resolve(undefined);
  });
}

/** @param {IDBDatabase} db @param {string} storeName @returns {Promise<[IDBValidKey, unknown][]>} */
async function readAll(db, storeName) {
  if (!db.objectStoreNames.contains(storeName)) return [];
  const store = db.transaction(storeName, 'readonly').objectStore(storeName);
  const [keys, values] = await Promise.all([settle(store.getAllKeys()), settle(store.getAll())]);
  return keys.map((key, i) => [key, values[i]]);
}

/**
 * Copies the old database's uploads into `db` (already open, with `storeName`) once. An
 * upload already in `db` wins. → true when nothing is left to carry over, false to retry.
 * @param {IDBDatabase} db
 * @param {{factory?: IDBFactory, from?: string, storeName?: string}} [options]
 * @returns {Promise<boolean>}
 */
export async function migrateLegacyUploads(db, { factory = globalThis.indexedDB, from = LEGACY_UPLOAD_DB, storeName = 'uploads' } = {}) {
  try {
    const done = db.transaction(storeName, 'readonly').objectStore(storeName).get(UPLOADS_MIGRATED);
    if (await settle(done)) return true;
    const legacy = await openExisting(factory, from);
    if (legacy === undefined) return false;
    /** @type {[IDBValidKey, unknown][]} */
    let records = [];
    if (legacy) {
      try { records = await readAll(legacy, storeName); } finally { legacy.close(); }
    }
    return await new Promise((resolve) => {
      const transaction = db.transaction(storeName, 'readwrite');
      const store = transaction.objectStore(storeName);
      // Checked again inside the write: another tab may have finished it meanwhile.
      const marker = store.get(UPLOADS_MIGRATED);
      marker.onsuccess = () => {
        if (marker.result) return;
        const keys = store.getAllKeys();
        keys.onsuccess = () => {
          const present = new Set(keys.result);
          for (const [key, value] of records) if (!present.has(key)) store.put(value, key);
          store.put({ from, at: Date.now() }, UPLOADS_MIGRATED);
        };
      };
      transaction.oncomplete = () => resolve(true);
      transaction.onerror = transaction.onabort = () => resolve(false);
    });
  } catch {
    return false;
  }
}
