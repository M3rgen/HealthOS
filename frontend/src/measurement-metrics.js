// The manual body measurements: label from the dictionary, unit symbol, and the one way their
// values are written ("%17,8", "74,6 kg"), so a percentage reads the same on every screen.
import { num, percent } from './format.js';
import { t } from './i18n/index.js';

/** @type {Record<string, {label: string, unit: string}>} */
export const METRICS = {
  height: { label: t.overview.metrics.height, unit: 'cm' },
  weight: { label: t.overview.metrics.weight, unit: 'kg' },
  body_fat: { label: t.overview.metrics.bodyFat, unit: '%' },
  muscle: { label: t.overview.metrics.muscle, unit: '%' },
};

/** @param {string} metric @param {number | string | null | undefined} value @param {number} [digits] */
export function measurementText(metric, value, digits = 1) {
  const unit = METRICS[metric]?.unit;
  return unit === '%' ? percent(value, digits) : `${num(value, digits)}${unit ? ` ${unit}` : ''}`;
}
