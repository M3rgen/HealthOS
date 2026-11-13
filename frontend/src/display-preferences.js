// Display preferences (time zone choice, body model rotation), shared by every device
// through /api/settings/display. localStorage keeps the offline copy under the keys the
// dashboard used before, so the first paint never waits for the server. Like the
// appearance selection (appearance.js): the server's copy wins when it is reachable; a
// change that could not reach it is marked pending and sent by the next sync that does
// (last write wins).
import { request } from './api.js';
import { migrateLegacyKeys } from './legacy-names.js';
import { stored } from './local-store.js';

export const SCHEMA_VERSION = 1;
const REMOTE_URL = '/api/settings/display';
const KEYS = { timezone: 'healthos.timezone.v2', motion: 'healthos.motion' };
// Same rule as TimeZoneChoice in app/api/settings.py; Intl then decides if the zone exists.
const ZONE = /^(auto|[A-Za-z][A-Za-z0-9_+-]*(\/[A-Za-z0-9_+-]+){0,2})$/;

/** @typedef {{timezone?: string, motion?: boolean}} DisplayChoice */
/** @typedef {'saving' | 'saved' | 'offline'} SaveStatus */

/**
 * Keeps only preferences this version understands: {timezone?, motion?}.
 * @param {{timezone?: unknown, motion?: unknown} | null | undefined} value unvalidated: stored in the browser or sent by the server
 * @param {(zone: string) => boolean} validZone
 * @returns {DisplayChoice}
 */
export function cleanPreferences(value, validZone) {
  /** @type {DisplayChoice} */
  const out = {};
  const zone = value?.timezone;
  if (typeof zone === 'string' && zone.length <= 64 && ZONE.test(zone) && (zone === 'auto' || validZone(zone)))
    out.timezone = zone;
  if (typeof value?.motion === 'boolean') out.motion = value.motion;
  return out;
}

/** This browser's copy of the preferences. @param {(zone: string) => boolean} validZone @param {string} pendingKey */
function createLocalCopy(validZone, pendingKey) {
  return {
    read() {
      const motion = stored(KEYS.motion);
      return cleanPreferences(
        { timezone: stored(KEYS.timezone), motion: motion === null ? undefined : motion === 'true' },
        validZone,
      );
    },
    /** @param {DisplayChoice} preferences */
    keep(preferences) {
      stored(KEYS.timezone, preferences.timezone ?? null);
      stored(KEYS.motion, preferences.motion === undefined ? null : String(preferences.motion));
    },
    /** Reads the mark without a value, writes it with one. @param {boolean} [value] */
    pending: value => (value === undefined ? stored(pendingKey) === '1' : stored(pendingKey, value ? '1' : null)),
  };
}

/**
 * Sends preferences to the server, one request at a time, newest last; superseded writes are skipped.
 * @param {{url: string, onStatus: (status: SaveStatus) => void, pending: (value?: boolean) => unknown}} parts
 */
function createSender({ url, onStatus, pending }) {
  /** @type {DisplayChoice | null} */
  let latest = null;
  /** @type {Promise<void>} */
  let queue = Promise.resolve();
  /** @param {DisplayChoice} preferences */
  const push = async preferences => {
    onStatus('saving');
    try {
      await request(url, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json', 'X-HealthOS-Request': '1' },
        body: JSON.stringify({ schema_version: SCHEMA_VERSION, preferences }),
      });
      if (latest === preferences) {
        pending(false);
        onStatus('saved');
      }
    } catch {
      onStatus('offline'); // Offline or rejected: stays pending; the local copy still applies.
    }
  };
  /** @param {DisplayChoice} preferences */
  return preferences => {
    latest = preferences;
    pending(true);
    queue = queue.then(() => (latest === preferences ? push(preferences) : undefined));
    return queue;
  };
}

/**
 * read() → this browser's copy, at once. write(preferences) stores it here and sends it.
 * sync() → the server's preferences, or null when it is unreachable or this browser's copy
 * was sent instead (an unsent change, or a server with nothing stored yet).
 * onStatus('saving' | 'saved' | 'offline') reports each send.
 */
/** @param {{validZone: (zone: string) => boolean, url?: string, pendingKey?: string, onStatus?: (status: SaveStatus) => void}} options */
export function createDisplayPreferences({
  validZone,
  url = REMOTE_URL,
  pendingKey = 'healthos.display.pending',
  onStatus = () => {},
}) {
  migrateLegacyKeys(); // Before the first read: keys kept under the old product name.
  const { read, keep, pending } = createLocalCopy(validZone, pendingKey);
  const send = createSender({ url, onStatus, pending });
  return {
    read,
    /** @param {DisplayChoice} preferences */
    write(preferences) {
      const clean = cleanPreferences(preferences, validZone);
      keep(clean);
      return send(clean);
    },
    /** @returns {Promise<DisplayChoice | null>} */
    async sync() {
      /** @type {import('./api-types').StoredPreferences} */
      let remote;
      try {
        remote = await request(url);
      } catch {
        return null;
      }
      const local = read(); // Read after the request: a change made meanwhile wins.
      if (pending() || (!remote?.updated_at && Object.keys(local).length)) {
        await send(local);
        return null;
      }
      if (remote?.schema_version !== SCHEMA_VERSION) return null;
      const preferences = cleanPreferences(remote.preferences, validZone);
      keep(preferences);
      return preferences;
    },
  };
}
