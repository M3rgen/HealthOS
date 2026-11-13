// The screenshot tool itself: synthetic mocks, desktop + mobile, both languages, byte repeatability.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { execFile } = require('node:child_process');
const { promisify } = require('node:util');
const { BASE, OUTPUT, run } = require('./lib.cjs');
const { LANGUAGES } = require('../../frontend/preview/shoot.cjs');
// Matrix extremes + common phone; 1920x1080 is also the largest, so keep 375x667 for byte repeats.
const VIEWPORTS = ['320x256', '375x667', '390x844', '1920x1080'];
const VIEWS = ['overview', 'measurements', 'sports', 'system'];
const CAPTURES = VIEWPORTS.length * VIEWS.length * LANGUAGES.length;
const execute = promisify(execFile);
run(async () => {
  const tool = path.join(__dirname, '../../frontend/preview/shoot.cjs');
  const directories = ['shoot-first', 'shoot-repeat'].map(name => path.join(OUTPUT, name));
  // An earlier full-matrix run may have left extra PNGs in this synthetic output directory.
  fs.rmSync(directories[0], { recursive: true, force: true });
  for (const [index, out] of directories.entries()) {
    const { stdout } = await execute(process.execPath, [tool, '--base', BASE, '--out', out,
      '--viewports', index ? '1920x1080,375x667' : VIEWPORTS.join(','),
      '--views', VIEWS.join(',')], { timeout: 600000 });
    assert.match(stdout, /PASS: no main-page overflow/);
  }
  assert.equal(fs.readdirSync(directories[0]).filter(name => name.endsWith('.png')).length, CAPTURES);
  const files = fs.readdirSync(directories[1]).filter(name => name.endsWith('.png')).sort();
  assert.equal(files.length, 16);
  for (const file of files) assert.ok(fs.readFileSync(path.join(directories[0], file))
    .equals(fs.readFileSync(path.join(directories[1], file))), `non-deterministic screenshot: ${file}`);
  console.log(`PASS ${CAPTURES} subset captures; 16 desktop/mobile captures repeat byte-for-byte in Turkish and English`);
});
