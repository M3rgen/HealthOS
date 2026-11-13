// Readable names for the API's data type and exercise codes. A code without a name is still
// shown as words ("swim-lengths-data" → "Swim lengths data"), never as the identifier itself.
import { t } from './i18n/index.js';

/** @param {string|null|undefined} code */
const words = code => {
  const text = String(code ?? '').replace(/[-_]+/g, ' ').trim().toLowerCase();
  return text ? text[0].toLocaleUpperCase() + text.slice(1) : '';
};

/** @param {string|null|undefined} code */
export const dataTypeLabel = code => /** @type {Record<string, string>} */ (t.common.dataTypes)[String(code)] ?? words(code);

/** Google and Garmin send enum codes (RUN_WALK); a typed name ("Yürüyüş") passes through. @param {string} value */
export const exerciseLabel = value => {
  if (!value || !/^[A-Z][A-Z0-9_]*$/.test(value)) return value;
  return /** @type {Record<string, string>} */ (t.workouts.exerciseTypes)[value] ?? words(value);
};

/**
 * A data source by its code (FITBIT, HEALTH_KIT …) in the interface's language. The server's own
 * label is in one language only ("Apple Sağlık"); it is the fallback for a code the dictionary lacks.
 * @param {string|null|undefined} code @param {string|null|undefined} [label]
 */
export const sourceLabel = (code, label) =>
  /** @type {Record<string, string>} */ (t.devices.platforms)[String(code)] ?? label ?? code ?? '';
