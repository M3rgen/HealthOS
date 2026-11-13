#!/usr/bin/env node
// The `strict` ratchet. tsconfig.json keeps strictNullChecks on for every module but not yet the
// remaining strict flags; this runs `tsc --strict` and compares the error count of each file
// against strict-baseline.json. No file may exceed its baseline, a file not listed there must have
// zero errors, and a file that got better must lower its baseline. So the total can only shrink.
//   node scripts/check-strict.mjs           check
//   node scripts/check-strict.mjs --update  rewrite the baseline to the current counts (never to raise one by hand)
import { execFileSync } from 'node:child_process';
import { readFileSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const FRONTEND = fileURLToPath(new URL('../', import.meta.url));
const BASELINE = path.join(FRONTEND, 'strict-baseline.json');
const tsc = createRequire(import.meta.url).resolve('typescript/bin/tsc');

let output;
try {
  output = execFileSync(process.execPath, [tsc, '-p', 'tsconfig.json', '--strict', '--pretty', 'false'], { cwd: FRONTEND, encoding: 'utf8' });
} catch (error) {
  output = error.stdout ?? '';
  if (!output) throw error;
}
const counts = {};
for (const line of output.split('\n')) {
  const file = /^(.+?)\(\d+,\d+\): error TS/.exec(line)?.[1];
  if (file) counts[file.replaceAll('\\', '/')] = (counts[file.replaceAll('\\', '/')] ?? 0) + 1;
}
const total = Object.values(counts).reduce((sum, count) => sum + count, 0);

if (process.argv.includes('--update')) {
  const sorted = Object.fromEntries(Object.entries(counts).sort(([a], [b]) => a.localeCompare(b)));
  writeFileSync(BASELINE, `${JSON.stringify(sorted, null, 2)}\n`);
  console.log(`strict baseline written: ${total} errors in ${Object.keys(counts).length} files`);
  process.exit(0);
}

const baseline = JSON.parse(readFileSync(BASELINE, 'utf8'));
const problems = [];
for (const [file, count] of Object.entries(counts)) {
  const allowed = baseline[file] ?? 0;
  if (count > allowed) problems.push(`${file}: ${count} strict errors, baseline allows ${allowed}`);
}
for (const [file, allowed] of Object.entries(baseline)) {
  if ((counts[file] ?? 0) < allowed) problems.push(`${file}: improved to ${counts[file] ?? 0} (baseline ${allowed}); lower it with --update`);
}
if (problems.length) {
  console.error(`strict ratchet: ${problems.length} file(s) off baseline:\n  ${problems.join('\n  ')}`);
  console.error(output.split('\n').filter(line => problems.some(problem => line.startsWith(problem.split(':')[0]))).slice(0, 40).join('\n'));
  process.exit(1);
}
console.log(`strict ratchet: ok, ${total} remaining strict errors in ${Object.keys(counts).length} files (baseline holds)`);
