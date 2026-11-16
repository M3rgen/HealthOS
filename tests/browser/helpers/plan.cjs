// A synthetic stateful server shared by pages: reloads and another device read the same state.
const assert = require('node:assert/strict');
const fx = require('../../../frontend/preview/fixtures.cjs');
const { mock } = require('./accessibility.cjs');

function server() {
  return { view: structuredClone(fx.plan), gets: 0, puts: [], failRead: false, failWrite: false, release: null };
}
async function mockPlan(page, remote) {
  await mock(page);
  await page.route('**/api/plan', route => {
    remote.gets++;
    return route.fulfill({ status: remote.failRead ? 503 : 200,
      json: remote.failRead ? { detail: 'Synthetic failure' } : remote.view });
  });
  await page.route('**/api/plan/state', async route => {
    assert.equal(route.request().method(), 'PUT');
    assert.equal(route.request().headers()['x-healthos-request'], '1');
    const body = route.request().postDataJSON();
    remote.puts.push(body);
    if (remote.release) await new Promise(resolve => { remote.release = resolve; });
    if (remote.failWrite) return route.fulfill({ status: 503, json: { detail: 'Synthetic failure' } });
    remote.view.state = body;
    return route.fulfill({ json: body });
  });
}
module.exports = { server, mockPlan };
