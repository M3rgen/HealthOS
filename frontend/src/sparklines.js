// Card sparklines: gradient bars with rounded tops and a smooth line with a soft area.
// preserveAspectRatio="none" stretches x, so strokes and dots use non-scaling-stroke.
// Every value is a number or dictionary text through escape()/num(); the output is safe markup.
import { escape, num } from './format.js';
import { t } from './i18n/index.js';

const { cards } = t.overview;

/** A smooth SVG path through [x, y] points (Catmull-Rom as cubic Béziers). @param {[number, number][]} p */
export const smooth = p =>
  p
    .map((pt, i) => {
      if (!i) return `M${pt[0]},${pt[1]}`;
      const a = p[i - 2] || p[i - 1],
        b = p[i - 1],
        d = p[i + 1] || pt;
      return `C${b[0] + (pt[0] - a[0]) / 6},${b[1] + (pt[1] - a[1]) / 6} ${pt[0] - (d[0] - b[0]) / 6},${pt[1] - (d[1] - b[1]) / 6} ${pt[0]},${pt[1]}`;
    })
    .join('');

/** @param {(number | null | undefined)[]} values @param {string} color @param {string} [alternate] @param {string[]} [labels] */
export function bars(values, color, alternate = color, labels) {
  if (!values.some(v => v != null)) return `<p class="empty-chart">${escape(cards.noChartData)}</p>`;
  const max = Math.max(...values.filter(v => v != null), 1),
    width = 240 / values.length,
    id = `bar-${color.replace(/\W/g, '')}`;
  /** @param {string} c @param {string} key */
  const fill = (c, key) =>
    `<linearGradient id="${id}-${key}" gradientUnits="userSpaceOnUse" x1="0" y1="4" x2="0" y2="41"><stop offset="0" style="stop-color:${c}"/>` +
      `<stop offset="1" style="stop-color:${c};stop-opacity:.38"/></linearGradient>`;
  /** @param {number} v @param {number} i */
  const bar = (v, i) => {
    const w = Math.max(width * (values.length > 12 ? 0.62 : 0.7), 1),
      x = i * width + (width - w) / 2,
      h = Math.max((v / max) * 35, 0),
      y = 41 - h,
      r = Math.min(1.6, w / 2, h),
      title = labels ? escape(labels[i]) : `${i}: ${num(v, 1)}`;
    if (h < 1)
      return `<rect x="${x}" y="40" width="${w}" height="1" fill="${i % 2 ? alternate : color}" opacity=".35"><title>${title}</title></rect>`;
    return `<path d="M${x},41V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V41Z" fill="url(#${id}-${i % 2 ? 'b' : 'a'})">` +
      `<title>${title}</title></path>`;
  };
  return `<svg viewBox="0 0 240 42" preserveAspectRatio="none" aria-hidden="true"><defs>${fill(color, 'a')}${fill(alternate, 'b')}</defs>` +
    `<path d="M0,41.5H240" stroke="var(--line)" vector-effect="non-scaling-stroke"/>${values.map((v, i) => (v == null ? '' : bar(v, i))).join('')}</svg>`;
}

/** @param {(number | null | undefined)[]} values */
export function line(values) {
  const present = values.filter(v => v != null);
  if (!present.length) return `<p class="empty-chart">${escape(cards.noChartData)}</p>`;
  const min = Math.min(...present) - 3,
    max = Math.max(...present) + 3;
  /** @type {[number, number][][]} */
  const paths = [];
  /** @type {[number, number][]} */
  let points = [];
  values.forEach((v, i) => {
    if (v == null) {
      if (points.length) paths.push(points);
      points = [];
    } else points.push([(i / 23) * 240, 38 - ((v - min) / (max - min)) * 32]);
  });
  if (points.length) paths.push(points);
  const finalPath = paths[paths.length - 1],
    last = finalPath[finalPath.length - 1],
    stroke = 'stroke="var(--heart-chart)" vector-effect="non-scaling-stroke" stroke-linecap="round"';
  return `<svg viewBox="0 0 240 42" preserveAspectRatio="none" aria-hidden="true"><defs><linearGradient id="line-heart-area" x1="0" y1="0" x2="0" y2="1">` +
    `<stop offset="0" style="stop-color:var(--heart-chart);stop-opacity:.26"/><stop offset="1" style="stop-color:var(--heart-chart);stop-opacity:0"/>` +
      `</linearGradient></defs><path d="M0,41.5H240" stroke="var(--line)" vector-effect="non-scaling-stroke"/>` +
      `${paths.map(p => (p.length === 1 ? '' : `<path d="${smooth(p)}V42H${p[0][0]}Z" fill="url(#line-heart-area)"/>` +
        `<path d="${smooth(p)}" fill="none" ${stroke} stroke-linejoin="round" stroke-width="2"/>`)).join('')}${paths
    .filter(p => p.length === 1)
    .map(p => `<path d="M${p[0][0]},${p[0][1]}h0" ${stroke} stroke-width="4"/>`)
    .join(
      '',
    )}<path d="M${last[0]},${last[1]}h0" ${stroke} stroke-width="11" opacity=".22"/><path d="M${last[0]},${last[1]}h0" ${stroke} stroke-width="5.5"/></svg>`;
}
