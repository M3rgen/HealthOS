// Apple Health import panel on the System page: upload the Health app's export.zip (or a bare
// export.xml) as the raw request body, follow the upload and then the server's progress, and
// show the Health Auto Export receiver's state. One import at a time; the panel stays usable
// on every viewport (long messages scroll inside the panel).
import { handleUnauthorized } from './api.js';

const IMPORT_URL = '/api/apple/import';
const STATUS_URL = '/api/apple/status';
const POLL_MS = 1000;
const MB = 1024 * 1024;

const count = value => Number(value ?? 0).toLocaleString('tr-TR');
const megabytes = value => (value / MB).toLocaleString('tr-TR', { maximumFractionDigits: value < 10 * MB ? 1 : 0 });

function contentType(file) {
  const name = file.name.toLowerCase();
  if (name.endsWith('.zip')) return 'application/zip';
  if (name.endsWith('.xml')) return 'application/xml';
  return 'application/octet-stream';
}

/** Total of a counter object ({Me: 1, ClinicalRecord: 3}) or a plain number. */
function total(value) {
  if (typeof value === 'number') return value;
  if (value && typeof value === 'object') return Object.values(value).reduce((sum, item) => sum + total(item), 0);
  return 0;
}

export function setupAppleImport({ request, getZone }) {
  const $ = id => document.getElementById(id);
  const file = $('apple-import-file');
  const start = $('apple-import-start');
  const bar = $('apple-import-progress');
  const status = $('apple-import-status');
  let busy = false;
  let timer = null;
  let polling = false;
  let uploaded = { loaded: 0, total: 0, done: false };

  const day = value => value
    ? new Date(value).toLocaleDateString('tr-TR', { day: 'numeric', month: 'long', year: 'numeric', timeZone: getZone() })
    : null;

  function say(text, warning = false) {
    status.textContent = text;
    status.classList.toggle('warning', warning);
  }

  function setBusy(value) {
    busy = value;
    file.disabled = value;
    start.disabled = value || !file.files?.length;
    start.setAttribute('aria-busy', String(value));
    $('apple-import-panel').setAttribute('aria-busy', String(value));
  }

  function receiverLine(receiver) {
    const line = $('apple-import-receiver');
    if (!receiver) { line.textContent = ''; return; }
    line.textContent = !receiver.enabled
      ? 'Health Auto Export alıcısı: kapalı.'
      : receiver.token_file_present
        ? 'Health Auto Export alıcısı: açık.'
        : 'Health Auto Export alıcısı: açık, ama belirteç dosyası yok.';
  }

  function summary(result) {
    const parts = [`${count(result.records_read)} kayıt okundu`, `${count(result.records_new)} yeni`];
    const skipped = total(result.summary?.skipped);
    if (skipped) parts.push(`${count(skipped)} atlanan öğe`);
    const exported = day(result.export_date ?? result.summary?.export_date);
    if (exported) parts.push(`dışa aktarma tarihi ${exported}`);
    return `İçe aktarma tamamlandı: ${parts.join(' · ')}.`;
  }

  function progressText(running) {
    const parts = [];
    if (uploaded.total && !uploaded.done) {
      const percent = Math.floor((uploaded.loaded / uploaded.total) * 100);
      parts.push(`Yükleniyor: %${percent} (${megabytes(uploaded.loaded)} / ${megabytes(uploaded.total)} MB)`);
    } else if (uploaded.done) {
      parts.push('Dosya gönderildi, işleniyor');
    }
    if (running) parts.push(`${count(running.records_read)} kayıt okundu, ${count(running.records_new)} yeni`);
    return parts.length ? `${parts.join(' · ')}…` : 'İçe aktarma başlıyor…';
  }

  async function poll() {
    if (polling) return;
    polling = true;
    try {
      const current = await request(STATUS_URL);
      receiverLine(current.receiver);
      if (busy) say(progressText(current.running));
    } catch {
      // A missed poll is harmless; the upload's own answer ends the import.
    } finally {
      polling = false;
    }
  }

  function stopPolling() {
    clearInterval(timer);
    timer = null;
  }

  function upload() {
    const chosen = file.files?.[0];
    if (!chosen || busy) return;
    setBusy(true);
    uploaded = { loaded: 0, total: chosen.size, done: false };
    bar.hidden = false;
    bar.value = 0;
    bar.max = 100;
    say(progressText(null));
    const xhr = new XMLHttpRequest();
    xhr.open('POST', IMPORT_URL);
    xhr.responseType = 'json';
    xhr.setRequestHeader('Content-Type', contentType(chosen));
    xhr.setRequestHeader('X-HealthOS-Request', '1');
    xhr.setRequestHeader('X-Apple-Export-Filename', encodeURIComponent(chosen.name));
    xhr.upload.onprogress = event => {
      if (!event.lengthComputable) return;
      uploaded.loaded = event.loaded;
      uploaded.total = event.total;
      bar.value = Math.floor((event.loaded / event.total) * 100);
      say(progressText(null));
    };
    xhr.upload.onload = () => {
      uploaded.done = true;
      bar.removeAttribute('value'); // Indeterminate while the server finishes.
      say(progressText(null));
    };
    const finish = (text, warning) => {
      stopPolling();
      bar.hidden = true;
      setBusy(false);
      say(text, warning);
    };
    xhr.onload = () => {
      if (handleUnauthorized(xhr)) { finish('Oturumun sona erdi. Giriş sayfasına yönlendiriliyorsun.', true); return; }
      const body = xhr.response;
      if (xhr.status >= 200 && xhr.status < 300 && body) {
        file.value = '';
        finish(summary(body), false);
        return;
      }
      const detail = typeof body?.detail === 'string' ? body.detail : null;
      const fallback = {
        409: 'Başka bir içe aktarma sürüyor. Bitince yeniden dene.',
        413: 'Dosya izin verilen boyutu aşıyor.',
        415: 'Yalnız export.zip veya export.xml yüklenebilir.',
      }[xhr.status];
      finish(`İçe aktarılamadı: ${detail ?? fallback ?? 'sunucu isteği tamamlayamadı.'}`, true);
    };
    xhr.onerror = () => finish('Sunucuya ulaşılamadı. Kaydedilen kayıtlar duruyor; dosyayı yeniden yükleyebilirsin.', true);
    xhr.onabort = () => finish('Yükleme durduruldu.', true);
    timer = setInterval(poll, POLL_MS);
    xhr.send(chosen);
  }

  async function refresh() {
    if (busy) return;
    try {
      const current = await request(STATUS_URL);
      if (busy) return;
      receiverLine(current.receiver);
      if (current.running) {
        say(`Bir içe aktarma sürüyor: ${count(current.running.records_read)} kayıt okundu, ${count(current.running.records_new)} yeni.`);
        start.disabled = true;
        return;
      }
      start.disabled = !file.files?.length;
      const last = current.imports?.find(item => item.channel === 'export');
      if (!status.textContent || status.dataset.source === 'status') {
        status.dataset.source = 'status';
        if (!last) say('');
        else if (last.status === 'done') say(`Son içe aktarma: ${day(last.finished_at ?? last.started_at)} · ${count(last.records_read)} kayıt, ${count(last.records_new)} yeni.`);
        else if (last.status === 'failed') say(`Son içe aktarma tamamlanamadı: ${last.error ?? 'bilinmeyen hata'}`, true);
        else say('');
      }
    } catch {
      receiverLine(null);
      $('apple-import-receiver').textContent = 'İçe aktarma durumu alınamadı.';
    }
  }

  file.addEventListener('change', () => { start.disabled = busy || !file.files?.length; });
  start.addEventListener('click', () => { status.dataset.source = 'upload'; upload(); });
  window.addEventListener('pagehide', stopPolling);
  return { refresh };
}
