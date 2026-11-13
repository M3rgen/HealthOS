import { gzipSync } from 'node:zlib';
import { existsSync, readFileSync, statSync } from 'node:fs';
import path from 'node:path';
import { stripComments } from './scripts/strip-comments.mjs';

export const baseline = JSON.parse(readFileSync(new URL('./performance-budget.json', import.meta.url)));
// Never silently raise a historical ceiling when the requested margin does not fit.
export const limits = Object.fromEntries(Object.entries(baseline.budgets).map(([key, value]) =>
  [key, Math.min(value.ceiling, Math.ceil(value.measured * (1 + value.headroomPercent / 100)))]));
const bytes = item => Buffer.from(item.type === 'chunk' ? item.code : item.source);
const bodyModule = id => /\/src\/body\.js$/.test(id.replaceAll('\\', '/'));
const threeModule = id => /\/node_modules\/three\//.test(id.replaceAll('\\', '/'));

// Files of /public a deferred chunk asks for at run time: the 3D model and its textures. They are
// the largest part of the page's download, so they are measured from the real references.
const PUBLIC_REFERENCE = /["'`](\/(?:models|assets)\/[\w./@-]+\.(?:glb|gltf|webp|png|jpe?g|ktx2|hdr))["'`]/g;
function figureAssets(chunks, publicDir) {
  if (!publicDir) return { bytes: 0, files: [] };
  const found = new Map();
  for (const code of chunks) {
    for (const [, reference] of code.matchAll(PUBLIC_REFERENCE)) {
      const file = path.join(publicDir, reference);
      if (!existsSync(file)) throw new Error(`Budget: deferred code references a missing file ${reference}`);
      found.set(reference, statSync(file).size);
    }
  }
  const files = [...found.keys()].sort();
  return { bytes: files.reduce((sum, file) => sum + found.get(file), 0), files };
}

// Inspect emitted HTML and the bundler's import metadata, never hashed name patterns or outDir.
export function measureBudget(bundle, base = '/', { publicDir } = {}) {
  const origin = 'https://build.invalid';
  const baseURL = new URL(base, origin);
  const resolve = (reference, from) => {
    const url = new URL(reference, new URL(from, baseURL));
    if (url.origin !== baseURL.origin || !url.pathname.startsWith(baseURL.pathname))
      throw new Error(`Budget: external dependency ${reference}`);
    const file = decodeURIComponent(url.pathname.slice(baseURL.pathname.length));
    if (!bundle[file]) throw new Error(`Budget: missing dependency ${file}`);
    return file;
  };
  const visit = (roots, dynamic = false) => {
    const seen = new Set();
    function walk(file) {
      if (seen.has(file)) return;
      const item = bundle[file];
      if (!item) throw new Error(`Budget: missing dependency ${file}`);
      seen.add(file);
      if (item.type === 'chunk') {
        for (const dependency of item.imports) walk(dependency);
        for (const css of item.viteMetadata?.importedCss ?? []) walk(css);
        if (dynamic) for (const dependency of item.dynamicImports) walk(dependency);
      } else if (file.endsWith('.css')) {
        const css = stripComments(bytes(item).toString(), '/*', '*/');
        const imports = /@import\s+(?:url\(\s*(?:"([^"]+)"|'([^']+)'|([^\s)]+))\s*\)|"([^"]+)"|'([^']+)')/gi;
        for (const match of css.matchAll(imports)) {
          const reference = match.slice(1).find(value => value !== undefined);
          walk(resolve(reference, file));
        }
      }
    }
    for (const root of roots) walk(root);
    return seen;
  };
  const roots = [];
  for (const [file, item] of Object.entries(bundle)) {
    if (!file.endsWith('.html')) continue;
    const html = stripComments(bytes(item).toString(), '<!--', '-->');
    for (const match of html.matchAll(/<(script|link)\b([^>]+)>/gi)) {
      const attributes = Object.fromEntries([...match[2].matchAll(/([\w-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))/g)]
        .map(([, key, double, single, bare]) => [key.toLowerCase(), double ?? single ?? bare]));
      const script = match[1].toLowerCase() === 'script' && attributes.type === 'module';
      const link = match[1].toLowerCase() === 'link' && /\b(modulepreload|stylesheet)\b/.test(attributes.rel);
      const reference = script ? attributes.src : link ? attributes.href : null;
      if (reference) roots.push(resolve(reference, file));
    }
  }
  if (!roots.length) throw new Error('Budget: no entry HTML dependencies');
  const initial = visit(roots);
  const chunks = Object.entries(bundle).filter(([, item]) => item.type === 'chunk');
  const protectedChunks = chunks.filter(([, item]) => Object.keys(item.modules).some(id => bodyModule(id) || threeModule(id)));
  for (const identify of [bodyModule, threeModule]) {
    if (!chunks.some(([, item]) => Object.keys(item.modules).some(identify)))
      throw new Error('Budget: body/three.js output is missing');
  }
  for (const [file] of protectedChunks) {
    if (initial.has(file)) throw new Error(`Budget: body/three.js entered the initial static graph: ${file}`);
  }
  const deferred = visit(protectedChunks.map(([file]) => file), true).difference(initial);
  function totals(files) {
    const result = { js: { raw: 0, gzip: 0, files: [] }, css: { raw: 0, gzip: 0, files: [] } };
    for (const file of [...files].sort()) {
      const kind = /\.(?:m?js)$/.test(file) ? 'js' : file.endsWith('.css') ? 'css' : null;
      if (!kind) continue; // Fonts, images and maps are outside the JS/CSS totals.
      const content = bytes(bundle[file]);
      result[kind].raw += content.length;
      result[kind].gzip += gzipSync(content, { level: 9 }).length;
      result[kind].files.push(file);
    }
    return result;
  }
  const deferredReport = totals(deferred);
  const figure = figureAssets(deferredReport.js.files.map(file => bytes(bundle[file]).toString()), publicDir);
  return { initial: totals(initial), deferred: deferredReport, figure };
}

const LIMITS = ['js', 'css', 'deferredJs', 'deferredCss', 'figureAssets'];
const REQUIRED_LIMITS = ['js', 'css'];

function checkLimits(budget) {
  for (const key of LIMITS) {
    if (budget[key] === undefined && !REQUIRED_LIMITS.includes(key)) continue;
    if (!Number.isSafeInteger(budget[key]) || budget[key] <= 0)
      throw new Error(`Budget: ${REQUIRED_LIMITS.includes(key) ? `${key.toUpperCase()} gzip` : key} limit must be a positive integer`);
  }
}

const groupLimit = (group, kind, budget) => (group === 'initial' ? budget[kind] : budget[`deferred${kind[0].toUpperCase()}${kind.slice(1)}`]);

function logGroup(group, values, budget, log) {
  for (const [kind, value] of Object.entries(values)) {
    const limit = groupLimit(group, kind, budget);
    log(`[budget] ${group} ${kind.toUpperCase()}: raw ${value.raw} B; gzip ${value.gzip} B${limit ? `; gzip limit ${limit} B (enforced)` : ''}`);
    log(`[budget]   ${value.files.join(', ') || '(none)'}`);
  }
}

function assertWithin(measured, limit, label) {
  if (limit !== undefined && measured > limit) throw new Error(`${label} budget exceeded: measured ${measured} B > limit ${limit} B`);
}

export function enforceBudget(report, budget = limits, log = console.log) {
  checkLimits(budget);
  const headroom = budget.js - report.initial.js.gzip;
  log(`[budget] Node ${process.versions.node}; zlib ${process.versions.zlib}; initial JS headroom ${headroom} B${headroom < 500 ? '; WARNING: below 500 B' : ''}`);
  logGroup('initial', report.initial, budget, log);
  logGroup('deferred', report.deferred, budget, log);
  const figure = report.figure ?? { bytes: 0, files: [] };
  log(`[budget] figure assets: ${figure.bytes} B${budget.figureAssets ? `; limit ${budget.figureAssets} B (enforced)` : ''}`);
  log(`[budget]   ${figure.files.join(', ') || '(none)'}`);
  for (const kind of ['js', 'css']) {
    assertWithin(report.initial[kind].gzip, budget[kind], `Initial ${kind.toUpperCase()} gzip`);
    assertWithin(report.deferred[kind].gzip, budget[`deferred${kind[0].toUpperCase()}${kind.slice(1)}`], `Deferred ${kind.toUpperCase()} gzip`);
  }
  assertWithin(figure.bytes, budget.figureAssets, 'Figure assets');
}

// publicDir: the /public directory whose files the deferred code references (Vite passes it).
export function performanceBudget() {
  let base, publicDir;
  return {
    name: 'healthos-performance-budget',
    apply: 'build',
    enforce: 'post',
    configResolved(config) { base = config.base; publicDir = config.publicDir; },
    generateBundle: {
      order: 'post',
      handler(_options, bundle) {
        const report = measureBudget(bundle, base, { publicDir });
        enforceBudget(report);
        this.emitFile({
          type: 'asset', fileName: 'performance-budget-report.json',
          source: JSON.stringify({ gzipLevel: 9, limits, ...report }, null, 2),
        });
      },
    },
  };
}
