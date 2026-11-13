// The browser storage behind the display and language preferences: one value per key in
// localStorage. Restricted storage (private mode, blocked site data) must never break the
// dashboard, so a failed read gives null and a failed write is dropped.
/** @returns {Storage|null} */
export function browserStorage() {
  try { return globalThis.localStorage ?? null; }
  catch { return null; }
}

/** @param {string} key @param {string|null} [value] */
export function stored(key, value) {
  try {
    const storage = browserStorage();
    if (!storage) return null;
    if (value === undefined) return storage.getItem(key);
    if (value === null) storage.removeItem(key);
    else storage.setItem(key, value);
  } catch {
    /* Restricted browser storage still allows using the app. */
  }
  return null;
}
