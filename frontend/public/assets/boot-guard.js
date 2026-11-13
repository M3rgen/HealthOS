// A static classic script, not part of the bundle: it must run even when the bundle's own files
// cannot be fetched, as after an update leaves an old tab pointing at chunks that no longer exist.
// It only watches the start: main.js sets <html data-booted> once every part of the page is wired,
// and failures after that are reported on the page itself. An inline handler would be blocked by
// the Content-Security-Policy, so this file is served from /assets like the rest of the static files.
(function () {
  var TEXTS = {
    tr: {
      title: 'HealthOS açılamadı',
      detail: 'Sayfanın bir parçası yüklenemedi; uygulama güncellenmiş ya da bağlantı kesilmiş olabilir. ' +
        'Sayfayı yenile; sorun sürerse sunucunun çalıştığını kontrol et. Sağlık verilerin bu hatadan etkilenmez.',
      reload: 'Sayfayı yenile',
    },
    en: {
      title: 'HealthOS could not open',
      detail: 'A part of the page could not be loaded; the app may have been updated or the connection dropped. ' +
        'Reload the page; if this continues, check that the server is running. Your health data is not affected by this error.',
      reload: 'Reload page',
    },
  };
  function language() {
    try { return localStorage.getItem('healthos.language') === 'en' ? 'en' : 'tr'; } catch { return 'tr'; }
  }
  var shown = false;
  function show() {
    if (shown || document.documentElement.dataset.booted) return;
    shown = true;
    var text = TEXTS[language()];
    var screen = document.createElement('div');
    screen.id = 'boot-failure';
    screen.setAttribute('role', 'alert');
    screen.style.cssText = 'position:fixed;inset:0;z-index:100;display:flex;flex-direction:column;justify-content:safe center;' +
      'align-items:center;gap:16px;padding:24px;overflow:auto;background:#181814;color:#f8f8f8;font:1rem/1.5 system-ui,sans-serif;text-align:center';
    var title = document.createElement('h1');
    title.textContent = text.title;
    title.style.cssText = 'margin:0;font-size:1.5rem;font-weight:650';
    var detail = document.createElement('p');
    detail.textContent = text.detail;
    detail.style.cssText = 'margin:0;max-width:34rem;color:#b9b7ae';
    var reload = document.createElement('button');
    reload.type = 'button';
    reload.textContent = text.reload;
    reload.style.cssText = 'min-height:44px;padding:8px 20px;border:0;border-radius:10px;background:#f0c478;color:#141413;font:inherit;font-weight:600;cursor:pointer';
    reload.addEventListener('click', function () { location.reload(); });
    screen.append(title, detail, reload);
    // The markup under it is empty and, without its stylesheet, taller than the screen.
    Array.prototype.forEach.call(document.body.children, function (element) { element.hidden = true; });
    document.body.append(screen);
    reload.focus();
  }
  // A module or stylesheet of the page that cannot be fetched: `error` on the element, caught on
  // the way down (it does not bubble). An exception while the modules start: `error` on window.
  window.addEventListener('error', function (event) {
    var target = event.target;
    if (target instanceof HTMLScriptElement || target instanceof HTMLLinkElement) {
      if (target.type === 'module' || target.rel === 'stylesheet' || target.rel === 'modulepreload') show();
    } else if (event.error) {
      show();
    }
  }, true);
  // Top-level await in main.js: a rejected start arrives here.
  window.addEventListener('unhandledrejection', show);
})();
