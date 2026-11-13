import assert from 'node:assert/strict';
import { test } from 'node:test';
import { paintLoginWarning } from '../../frontend/src/login-warning.js';

test('disabled login warning persists across incomplete health updates and clears with authentication', () => {
  const banner = { hidden: true };
  paintLoginWarning({ ui_login_disabled: false }, banner);
  assert.equal(banner.hidden, true);
  paintLoginWarning({ ui_login_disabled: true, database: 'unavailable' }, banner);
  assert.equal(banner.hidden, false);
  paintLoginWarning({}, banner);
  assert.equal(banner.hidden, false);
  paintLoginWarning({ ui_login_disabled: false }, banner);
  assert.equal(banner.hidden, true);
});
