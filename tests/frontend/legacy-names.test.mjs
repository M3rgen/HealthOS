import test from 'node:test';
import assert from 'node:assert/strict';
import {
  KEYS_MIGRATED, LEGACY_KEYS, LEGACY_UPLOAD_DB, UPLOADS_MIGRATED, migrateLegacyKeys, migrateLegacyUploads,
} from '../../frontend/src/legacy-names.js';
import { createLocalAppearanceStore } from '../../frontend/src/appearance.js';

function memoryStorage(entries = {}) {
  const items = new Map(Object.entries(entries));
  return {
    items,
    getItem: key => (items.has(key) ? items.get(key) : null),
    setItem: (key, value) => items.set(key, String(value)),
    removeItem: key => items.delete(key),
  };
}

// A small in-memory IndexedDB: enough of open/upgrade/transactions/stores for these tests.
function fakeIndexedDB(initial = {}) {
  const databases = new Map();
  const later = fn => setTimeout(fn, 0);
  const makeDb = (name, stores) => ({
    name,
    stores,
    objectStoreNames: { contains: store => stores.has(store) },
    createObjectStore(store) { stores.set(store, new Map()); },
    close() {},
    transaction(storeName, mode = 'readonly') {
      if (!stores.has(storeName)) throw new DOMException('no store', 'NotFoundError');
      const data = stores.get(storeName);
      let pending = 0, finished = false;
      const transaction = { oncomplete: null, onerror: null, onabort: null };
      const settle = () => later(() => {
        if (pending || finished) return;
        finished = true;
        transaction.oncomplete?.();
      });
      const request = (work) => {
        const req = { onsuccess: null, onerror: null, result: undefined };
        pending += 1;
        later(() => {
          req.result = work();
          pending -= 1;
          req.onsuccess?.();
          settle();
        });
        return req;
      };
      const write = (fn) => {
        if (mode !== 'readwrite') throw new DOMException('read only', 'ReadOnlyError');
        return request(fn);
      };
      const keys = () => [...data.keys()].sort();
      transaction.objectStore = () => ({
        get: key => request(() => (data.has(key) ? structuredClone(data.get(key)) : undefined)),
        getAllKeys: () => request(keys),
        getAll: () => request(() => keys().map(key => structuredClone(data.get(key)))),
        put: (value, key) => write(() => { data.set(key, structuredClone(value)); return key; }),
        delete: key => write(() => { data.delete(key); }),
      });
      settle();
      return transaction;
    },
  });
  for (const [name, records] of Object.entries(initial)) {
    databases.set(name, makeDb(name, new Map([['uploads', new Map(Object.entries(records))]])));
  }
  const factory = {
    databases,
    opened: [],
    open(name, version) {
      factory.opened.push(name);
      const req = { onsuccess: null, onerror: null, onupgradeneeded: null, onblocked: null, result: null, transaction: null };
      later(() => {
        if (databases.has(name)) {
          req.result = databases.get(name);
          req.onsuccess?.();
          return;
        }
        const db = makeDb(name, new Map());
        let aborted = false;
        req.result = db;
        req.transaction = { abort() { aborted = true; } };
        req.onupgradeneeded?.({ oldVersion: 0, newVersion: version ?? 1 });
        if (aborted) {
          req.result = null;
          req.onerror?.({ preventDefault() {} });
          return;
        }
        databases.set(name, db);
        req.onsuccess?.();
      });
      return req;
    },
  };
  return factory;
}

async function openNew(factory, name = 'healthos-appearance') {
  return new Promise((resolve) => {
    const req = factory.open(name, 1);
    req.onupgradeneeded = () => req.result.createObjectStore('uploads');
    req.onsuccess = () => resolve(req.result);
  });
}

const upload = (kind, text) => ({ kind, name: `${kind}.png`, files: { mark: new Blob([text]) } });
const records = (factory, name) => factory.databases.get(name)?.stores.get('uploads');

test('old localStorage keys are carried over once and never removed', () => {
  const old = Object.fromEntries(Object.values(LEGACY_KEYS).map((key, i) => [key, `v${i}`]));
  const storage = memoryStorage(old);
  migrateLegacyKeys(storage);
  Object.entries(LEGACY_KEYS).forEach(([key, legacy]) => {
    assert.equal(storage.getItem(key), old[legacy]);
    assert.equal(storage.getItem(legacy), old[legacy]);
  });
  assert.equal(storage.getItem(KEYS_MIGRATED), '1');
});

test('a value already under the new key wins over the old one', () => {
  const storage = memoryStorage({ 'healtos.motion': 'false', 'healthos.motion': 'true' });
  migrateLegacyKeys(storage);
  assert.equal(storage.getItem('healthos.motion'), 'true');
  assert.equal(storage.getItem('healtos.motion'), 'false');
});

test('a key removed after the carry-over does not come back from the old copy', () => {
  const storage = memoryStorage({ 'healtos.display.pending': '1' });
  migrateLegacyKeys(storage);
  storage.removeItem('healthos.display.pending');
  migrateLegacyKeys(storage);
  assert.equal(storage.getItem('healthos.display.pending'), null);
  assert.equal(storage.getItem('healtos.display.pending'), '1');
});

test('restricted storage is not an error', () => {
  const broken = { getItem() { throw new Error('denied'); }, setItem() { throw new Error('denied'); } };
  assert.doesNotThrow(() => migrateLegacyKeys(broken));
  assert.doesNotThrow(() => migrateLegacyKeys(null));
});

test('uploads in the old database are copied into an empty new one; the old one stays', async () => {
  const factory = fakeIndexedDB({ [LEGACY_UPLOAD_DB]: { logo: upload('logo', 'L'), umay: upload('umay', 'U') } });
  const db = await openNew(factory);
  assert.equal(await migrateLegacyUploads(db, { factory }), true);
  const copied = records(factory, 'healthos-appearance');
  assert.equal(await copied.get('logo').files.mark.text(), 'L');
  assert.equal(await copied.get('umay').files.mark.text(), 'U');
  assert.ok(copied.get(UPLOADS_MIGRATED));
  assert.deepEqual([...records(factory, LEGACY_UPLOAD_DB).keys()], ['logo', 'umay']);
});

test('an upload already in the new database wins, and the copy runs only once', async () => {
  const factory = fakeIndexedDB({
    [LEGACY_UPLOAD_DB]: { logo: upload('logo', 'old'), background: upload('background', 'B') },
    'healthos-appearance': { logo: upload('logo', 'new') },
  });
  const db = factory.databases.get('healthos-appearance');
  assert.equal(await migrateLegacyUploads(db, { factory }), true);
  const current = records(factory, 'healthos-appearance');
  assert.equal(await current.get('logo').files.mark.text(), 'new');
  assert.equal(await current.get('background').files.mark.text(), 'B');

  current.delete('background'); // Removed by the user later: must not return.
  assert.equal(await migrateLegacyUploads(db, { factory }), true);
  assert.equal(current.has('background'), false);
  assert.equal(records(factory, LEGACY_UPLOAD_DB).size, 2);
});

test('a browser without the old database gets none created', async () => {
  const factory = fakeIndexedDB();
  const db = await openNew(factory);
  assert.equal(await migrateLegacyUploads(db, { factory }), true);
  assert.equal(factory.databases.has(LEGACY_UPLOAD_DB), false);
  assert.ok(records(factory, 'healthos-appearance').get(UPLOADS_MIGRATED));
});

test('the appearance store finds old uploads and selection on its first read', async (t) => {
  const saved = ['localStorage', 'indexedDB'].map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]);
  t.after(() => saved.forEach(([key, descriptor]) => {
    if (descriptor) Object.defineProperty(globalThis, key, descriptor);
    else delete globalThis[key];
  }));
  const factory = fakeIndexedDB({ [LEGACY_UPLOAD_DB]: { umay: upload('umay', 'portrait') } });
  const storage = memoryStorage({ 'healtos.appearance': '{"logo":"yurt","umay":"upload"}' });
  Object.defineProperty(globalThis, 'indexedDB', { value: factory, configurable: true, writable: true });
  Object.defineProperty(globalThis, 'localStorage', { value: storage, configurable: true, writable: true });

  const store = createLocalAppearanceStore();
  assert.deepEqual(await store.getSelection(), { logo: 'yurt', umay: 'upload' });
  assert.equal(await (await store.getUpload('umay')).files.mark.text(), 'portrait');
  assert.equal(await store.getUpload('logo'), null);
  assert.equal(storage.getItem('healtos.appearance'), '{"logo":"yurt","umay":"upload"}');
  assert.ok(factory.databases.has(LEGACY_UPLOAD_DB));
});
