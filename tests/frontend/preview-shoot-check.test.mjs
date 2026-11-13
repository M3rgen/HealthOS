import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { promisify } from 'node:util';
import { runInNewContext } from 'node:vm';

const file = new URL('../browser/preview-shoot.cjs', import.meta.url);
const require = createRequire(file);
const { args, LANGUAGES, VIEWPORTS } = require('../../frontend/preview/shoot.cjs');

// Exercise the check's actual calls and assertions without launching a browser or writing PNGs.
async function check({ stdout = 'PASS: no main-page overflow', changedBytes = false } = {}) {
  const calls = [], logs = [];
  const directories = new Map([[path.join('synthetic-output', 'shoot-first'), new Map([['old-matrix.png', Buffer.from('stale')]])]]);
  const execFile = () => { throw new Error('expected promisified execFile'); };
  execFile[promisify.custom] = async (executable, argv) => {
    assert.equal(executable, process.execPath);
    const opt = args(argv.slice(1)), files = directories.get(opt.out) || new Map();
    for (const viewport of opt.viewports) for (const language of LANGUAGES) for (const view of opt.views) {
      const name = `${viewport}-${language}-${view}.png`;
      files.set(name, Buffer.from(changedBytes && calls.length ? 'changed' : name));
    }
    calls.push(opt);
    directories.set(opt.out, files);
    return { stdout };
  };
  let done;
  runInNewContext(readFileSync(file, 'utf8'), {
    __dirname: path.dirname(fileURLToPath(file)), process, console: { log: line => logs.push(line) },
    require: name => {
      if (name === './lib.cjs') return { BASE: 'http://127.0.0.1:5174', OUTPUT: 'synthetic-output', run: main => { done = main(); } };
      if (name === 'node:child_process') return { execFile };
      if (name === 'node:fs') return {
        rmSync: directory => directories.delete(directory),
        readdirSync: directory => [...directories.get(directory).keys()],
        readFileSync: filename => directories.get(path.dirname(filename)).get(path.basename(filename)),
      };
      return require(name);
    },
  });
  await done;
  return { calls, logs };
}

test('browser screenshot check replaces old matrix output with four sizes and both-language byte repeats', async () => {
  const { calls, logs } = await check();
  assert.equal(calls.length, 2);
  assert.deepEqual(calls[0].viewports, [VIEWPORTS[0], '375x667', '390x844', VIEWPORTS.at(-1)]);
  assert.equal(new Set(calls[0].viewports).size, 4);
  assert.ok(calls[0].viewports.every(size => VIEWPORTS.includes(size)));
  assert.deepEqual(calls[1].viewports, ['1920x1080', '375x667']);
  for (const call of calls) assert.deepEqual(call.views, ['overview', 'measurements', 'sports', 'system']);
  assert.deepEqual(LANGUAGES, ['tr', 'en']);
  assert.match(logs[0], /^PASS 32 subset captures; 16 desktop\/mobile captures repeat byte-for-byte in Turkish and English$/);
});

test('browser screenshot check still rejects missing overflow success and changed repeat bytes', async () => {
  await assert.rejects(check({ stdout: 'FAIL: page overflow or problems detected' }), /PASS: no main-page overflow/);
  await assert.rejects(check({ changedBytes: true }), /non-deterministic screenshot:/);
});
