// Apple Health import panel text (GitHub-only Apple extension), in both languages. Kept apart
// from tr.js and en.js so that it loads with the panel (apple-import.js, outside the initial
// bundle; docs/PERFORMANCE_BUDGET.md) instead of with every page. Complete by contract:
// tests/frontend/apple-i18n.test.mjs checks that both languages have the same keys and types.
/** @param {string|number} count @param {string} one @param {string} other */
const plural = (count, one, other) => `${count} ${Number(String(count).replace(/,/g, '')) === 1 ? one : other}`;

/** @typedef {string | number} AppleArg */
/** @typedef {string | ((...args: AppleArg[]) => string)} AppleText */

export const tr = /** @satisfies {Record<string, AppleText>} */ ({
  receiverOff: 'Health Auto Export alıcısı: kapalı.',
  receiverOn: 'Health Auto Export alıcısı: açık.',
  receiverNoToken: 'Health Auto Export alıcısı: açık, ama belirteç dosyası yok.',
  recordsRead: count => `${count} kayıt okundu`,
  recordsNew: count => `${count} yeni`,
  skipped: count => `${count} atlanan öğe`,
  exportDate: date => `dışa aktarma tarihi ${date}`,
  done: parts => `İçe aktarma tamamlandı: ${parts}.`,
  uploading: (percent, loaded, total) => `Yükleniyor: %${percent} (${loaded} / ${total} MB)`,
  sent: 'Dosya gönderildi, işleniyor',
  progressRecords: (read, fresh) => `${read} kayıt okundu, ${fresh} yeni`,
  starting: 'İçe aktarma başlıyor…',
  sessionExpired: 'Oturumun sona erdi. Giriş sayfasına yönlendiriliyorsun.',
  failed: detail => `İçe aktarılamadı: ${detail}`,
  conflict: 'Başka bir içe aktarma sürüyor. Bitince yeniden dene.',
  tooLarge: 'Dosya izin verilen boyutu aşıyor.',
  wrongType: 'Yalnız export.zip veya export.xml yüklenebilir.',
  serverFailed: 'sunucu isteği tamamlayamadı.',
  unreachable: 'Sunucuya ulaşılamadı. Kaydedilen kayıtlar duruyor; dosyayı yeniden yükleyebilirsin.',
  aborted: 'Yükleme durduruldu.',
  running: (read, fresh) => `Bir içe aktarma sürüyor: ${read} kayıt okundu, ${fresh} yeni.`,
  last: (day, read, fresh) => `Son içe aktarma: ${day} · ${read} kayıt, ${fresh} yeni.`,
  lastFailed: error => `Son içe aktarma tamamlanamadı: ${error}`,
  unknownError: 'bilinmeyen hata',
  statusFailed: 'İçe aktarma durumu alınamadı.',
});

/** @type {typeof tr} */
export const en = {
  receiverOff: 'Health Auto Export receiver: off.',
  receiverOn: 'Health Auto Export receiver: on.',
  receiverNoToken: 'Health Auto Export receiver: on, but the token file is missing.',
  recordsRead: count => `${plural(count, 'record', 'records')} read`,
  recordsNew: count => `${count} new`,
  skipped: count => `${plural(count, 'item', 'items')} skipped`,
  exportDate: date => `exported on ${date}`,
  done: parts => `Import finished: ${parts}.`,
  uploading: (percent, loaded, total) => `Uploading: ${percent}% (${loaded} / ${total} MB)`,
  sent: 'File sent, processing',
  progressRecords: (read, fresh) => `${plural(read, 'record', 'records')} read, ${fresh} new`,
  starting: 'Starting the import…',
  sessionExpired: 'Your session has ended. Taking you to the sign-in page.',
  failed: detail => `Could not import: ${detail}`,
  conflict: 'Another import is running. Try again when it has finished.',
  tooLarge: 'The file is larger than the allowed size.',
  wrongType: 'Only export.zip or export.xml can be uploaded.',
  serverFailed: 'the server could not complete the request.',
  unreachable: 'Could not reach the server. The records already saved are kept; you can upload the file again.',
  aborted: 'Upload stopped.',
  running: (read, fresh) => `An import is running: ${plural(read, 'record', 'records')} read, ${fresh} new.`,
  last: (day, read, fresh) => `Last import: ${day} · ${plural(read, 'record', 'records')}, ${fresh} new.`,
  lastFailed: error => `The last import did not finish: ${error}`,
  unknownError: 'unknown error',
  statusFailed: 'Could not get the import status.',
};

