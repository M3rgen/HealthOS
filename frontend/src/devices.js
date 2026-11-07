import './styles/devices.css';
import { setupDialog } from './accessibility.js';
import { relativeTime } from './relative-time.js';

// Status text travels with its color: the colour alone never carries the meaning.
const STATUS = { bagli: ['Bağlı', 'good'], gecikmeli: ['Gecikmeli', 'warning'], kopuk: ['Kopuk', 'bad'] };
const PLATFORMS = { FITBIT: 'Fitbit', HEALTH_KIT: 'Apple Sağlık', HEALTH_CONNECT: 'Health Connect', GOOGLE_FIT: 'Google Fit', GARMIN: 'Garmin' };
// Precedence picks which source is shown first; it never sums or hides the others.
const RANKS = { primary: 'öncelikli', secondary: 'ikincil', unranked: 'sırasız' };
const precedenceText = device => device.precedence
  ? `Aktivite: ${RANKS[device.precedence.activity] ?? 'sırasız'} · Sağlık: ${RANKS[device.precedence.health] ?? 'sırasız'}` : null;
const FORMS = { WATCH: 'Saat', TRACKER: 'Bileklik', PHONE: 'Telefon', SCALE: 'Tartı', RING: 'Yüzük', TABLET: 'Tablet', HEAD_MOUNTED: 'Kulaklık/Gözlük', CHEST_STRAP: 'Göğüs bandı', UNKNOWN: 'Bilinmiyor' };
const METHODS = { DERIVED: 'Cihaz hesapladı', AUTOMATICALLY_RECORDED: 'Otomatik kayıt', PASSIVELY_MEASURED: 'Pasif ölçüm', ACTIVELY_RECORDED: 'Kullanıcı başlattı', MANUALLY_ENTERED: 'Elle girildi' };
const label = (table, value) => (value ? table[value] ?? value : null);

export function setupDevices({ request, getZone, escape }) {
  const $ = id => document.getElementById(id);
  const dialog = $('devices-dialog');
  const modal = setupDialog(dialog);
  let report = null;
  let loading = false;
  const full = value => (value ? new Date(value).toLocaleString('tr-TR', { timeZone: getZone(), dateStyle: 'long', timeStyle: 'short' }) : null);
  const when = (value, fallback = 'Yok') => {
    const relative = relativeTime(value);
    return relative ? `<time datetime="${escape(value)}" title="${escape(full(value))}">${escape(relative)}</time>` : escape(fallback);
  };
  const kind = device => [label(PLATFORMS, device.platform), label(FORMS, device.form_factor ?? device.google_device_type)].filter(Boolean).join(' · ');

  function row(device) {
    const [text, tone] = STATUS[device.status] ?? ['Bilinmiyor', 'warning'];
    const google = device.google_last_sync ? `<span>Google son eşitleme: ${when(device.google_last_sync)}</span>` : '';
    const pull = device.garmin_last_pull ? `<span>Son çekim: ${when(device.garmin_last_pull)}</span>` : '';
    const rank = precedenceText(device) ? `<span class="device-precedence">${escape(precedenceText(device))}</span>` : '';
    return `<li class="device-row" data-status="${escape(device.status)}">
      <div class="device-main"><span class="device-name">${escape(device.name)}</span>${kind(device) ? `<span class="device-kind">${escape(kind(device))}</span>` : ''}<span class="dot ${tone}">${text}</span></div>
      <div class="device-meta"><span>Son veri: ${when(device.last_data, 'tarihli kayıt yok')}</span><span>${device.data_type_count} veri türü</span>${google}${pull}${rank}</div>
      <button type="button" class="button outline device-details" data-device="${escape(device.id)}" aria-haspopup="dialog" aria-label="${escape(device.name)} ayrıntıları">Ayrıntılar</button>
    </li>`;
  }

  function summary(devices, thresholds) {
    const count = status => devices.filter(device => device.status === status).length;
    const parts = Object.entries(STATUS).map(([status, [text]]) => (count(status) ? `${count(status)} ${text.toLocaleLowerCase('tr-TR')}` : null)).filter(Boolean);
    $('devices-summary').textContent = `${devices.length} cihaz · ${parts.join(' · ')}`;
    $('devices-thresholds').textContent = `Bağlı: son veri ${thresholds.connected_hours} saat içinde · Gecikmeli: ${thresholds.delayed_days} güne kadar · Kopuk: daha eski ya da tarihli kaydı yok. Son veri, kaydın kendi zamanıdır; eşitleme zamanı değil.`;
  }

  function render() {
    const { devices, thresholds } = report;
    $('devices-google-note').hidden = report.google !== 'unavailable';
    // An empty list is hidden rather than left as a zero-height focusable region.
    $('devices-list').hidden = !devices.length;
    if (!devices.length) {
      $('devices-summary').textContent = 'Henüz cihaz görülmedi.';
      $('devices-thresholds').textContent = 'Google’dan ya da Garmin’den ilk kayıtlar gelince cihazlar burada listelenir.';
      $('devices-list').innerHTML = '';
      return;
    }
    summary(devices, thresholds);
    $('devices-list').innerHTML = devices.map(row).join('');
  }

  function detail(term, value) {
    return value ? `<div><dt>${escape(term)}</dt><dd>${value}</dd></div>` : '';
  }

  function openDetails(id, trigger) {
    const device = report?.devices.find(item => item.id === id);
    if (!device) return;
    const [text] = STATUS[device.status] ?? ['Bilinmiyor'];
    const stamp = value => (value ? `${escape(full(value))}${relativeTime(value) ? ` · ${escape(relativeTime(value))}` : ''}` : null);
    $('devices-dialog-title').textContent = device.name;
    $('devices-details').innerHTML = [
      detail('Durum', escape(text)),
      detail('Platform', escape(label(PLATFORMS, device.platform))),
      detail('Cihaz türü', escape(label(FORMS, device.form_factor ?? device.google_device_type))),
      detail('Üretici', escape(device.manufacturer)),
      detail('Uygulama', escape(device.application)),
      detail('Kayıt yöntemi', escape(label(METHODS, device.recording_method))),
      detail('Öncelik', escape(precedenceText(device))),
      detail('Son veri', stamp(device.last_data) ?? 'Tarihli kayıt yok'),
      detail('Google son eşitleme', stamp(device.google_last_sync)),
      detail('Son çekim', stamp(device.garmin_last_pull)),
      detail('İlk görülme', stamp(device.first_seen)),
      detail('Son görülme', stamp(device.last_seen)),
      detail(`Veri türleri (${device.data_type_count})`, `<ul class="device-types">${device.data_types.map(type => `<li>${escape(type)}</li>`).join('')}</ul>`),
      detail('Eski adlar', device.previous_names.length ? escape(device.previous_names.join(', ')) : null),
      detail('Kaynak kimlikleri', escape(device.fingerprints.join(', '))),
    ].join('');
    modal.open({ trigger, initial: $('devices-close') });
  }

  async function refresh() {
    if (loading) return;
    loading = true;
    $('devices-summary').textContent = 'Cihazlar yükleniyor…';
    $('devices-list').setAttribute('aria-busy', 'true');
    $('devices-retry').hidden = true;
    try {
      report = await request('/api/devices');
      render();
    } catch {
      report = null;
      $('devices-list').innerHTML = '';
      $('devices-list').hidden = true;
      $('devices-google-note').hidden = true;
      $('devices-thresholds').textContent = '';
      $('devices-summary').textContent = 'Cihaz listesi alınamadı.';
      $('devices-retry').hidden = false;
    } finally {
      loading = false;
      $('devices-list').setAttribute('aria-busy', 'false');
    }
  }

  $('devices-list').addEventListener('click', event => {
    const button = event.target.closest('[data-device]');
    if (button) openDetails(button.dataset.device, button);
  });
  $('devices-close').addEventListener('click', () => dialog.close());
  $('devices-retry').addEventListener('click', refresh);
  return { refresh };
}
