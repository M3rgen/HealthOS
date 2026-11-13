/** @param {{ui_login_disabled?: boolean}} health @param {HTMLElement} [banner] */
export function paintLoginWarning(health, banner = /** @type {HTMLElement} */ (document.getElementById('login-disabled-warning'))) {
  if (typeof health.ui_login_disabled === 'boolean') banner.hidden = !health.ui_login_disabled;
}
