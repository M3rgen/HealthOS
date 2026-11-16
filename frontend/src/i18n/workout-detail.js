// Loaded with the workout detail view, including its translations, outside the first-render graph.

const tr = {
  region: 'Antrenman ayrıntısı', back: 'Zaman çizgisine dön', loading: 'Antrenman izi yükleniyor…',
  noTrack: 'Bu antrenmanın izi yok.', gone: 'Bu antrenman artık listede yok; liste yenilendi.',
  timeout: 'İz zamanında okunamadı.', failed: 'İz alınamadı.', retry: 'Yeniden dene',
  map: 'Rota haritası', route: 'Rota çizimi', start: 'Başlangıç', end: 'Bitiş', lapEdge: 'Tur sınırı', pause: 'Duraklatma',
  fit: 'Rotaya sığdır', zoomIn: 'Yakınlaştır', zoomOut: 'Uzaklaştır', attribution: 'OpenStreetMap katkıcıları', basemap: 'Protomaps',
  mapLoading: 'Harita açılıyor…',
  noTiles: 'Harita altlığı kurulu değil; rota altlıksız çizildi.',
  outside: 'Rota, kurulu harita altlığının dışında kalıyor; rota altlıksız çizildi.',
  noWebgl: 'Bu tarayıcıda WebGL2 yok; rota altlıksız çizildi.',
  mapFailed: 'Harita açılamadı; rota altlıksız çizildi.',
  noPosition: 'Bu antrenmanda konum kaydı yok; yalnız grafikler gösteriliyor.',
  summary: 'Özet', axis: 'Yatay eksen', axisTime: 'Zaman', axisDistance: 'Mesafe',
  speedShown: 'Hız gösterimi', speed: 'Hız', pace: 'Tempo',
  cursor: 'Grafikler: ok tuşlarıyla antrenman boyunca ilerle',
  thinned: (/** @type {string} */ shown, /** @type {string} */ total) => `Grafikler ${total} kayıttan seçilen ${shown} noktayla çizildi.`,
  series: { speed: 'Hız', pace: 'Tempo', heart_rate: 'Nabız', altitude: 'İrtifa', cadence: 'Kadans', power: 'Güç', temperature: 'Sıcaklık' },
  units: { speed: 'km/sa', pace: 'dk/km', heart_rate: 'bpm', altitude: 'm', cadence: 'rpm', power: 'W', temperature: '°C', km: 'km' },
  elapsed: 'Toplam süre', moving: 'Hareketli süre', distance: 'Mesafe', averageSpeed: 'Ortalama hız', maximumSpeed: 'Azami hız',
  averagePace: 'Ortalama tempo', bestPace: 'En iyi tempo', ascent: 'Tırmanış', descent: 'İniş',
  averageHeart: 'Ortalama nabız', maximumHeart: 'Azami nabız',
  laps: 'Turlar', lap: 'Tur', lapNumber: (/** @type {string} */ n) => `Tur ${n}`, lapTime: 'Süre', lapDistance: 'Mesafe', lapAverageHeart: 'Ort. nabız',
  lapMaximumHeart: 'Azami nabız',
};
/** @type {typeof tr} */
const en = {
  region: 'Workout detail', back: 'Back to the timeline', loading: 'Loading the workout track…',
  noTrack: 'This workout has no track.', gone: 'This workout is no longer listed; the list was refreshed.',
  timeout: 'The track could not be read in time.', failed: 'Could not load the track.', retry: 'Try again',
  map: 'Route map', route: 'Route drawing', start: 'Start', end: 'Finish', lapEdge: 'Lap boundary', pause: 'Pause',
  fit: 'Fit to route', zoomIn: 'Zoom in', zoomOut: 'Zoom out', attribution: 'OpenStreetMap contributors', basemap: 'Protomaps',
  mapLoading: 'Opening the map…',
  noTiles: 'No basemap is installed; the route is drawn on its own.',
  outside: 'The route lies outside the installed basemap; it is drawn on its own.',
  noWebgl: 'This browser has no WebGL2; the route is drawn on its own.',
  mapFailed: 'The map could not open; the route is drawn on its own.',
  noPosition: 'This workout recorded no positions; only the charts are shown.',
  summary: 'Summary', axis: 'Horizontal axis', axisTime: 'Time', axisDistance: 'Distance',
  speedShown: 'Speed shown as', speed: 'Speed', pace: 'Pace',
  cursor: 'Charts: move through the workout with the arrow keys',
  thinned: (/** @type {string} */ shown, /** @type {string} */ total) => `The charts draw ${shown} points chosen from ${total} records.`,
  series: { speed: 'Speed', pace: 'Pace', heart_rate: 'Heart rate', altitude: 'Elevation', cadence: 'Cadence', power: 'Power', temperature: 'Temperature' },
  units: { speed: 'km/h', pace: 'min/km', heart_rate: 'bpm', altitude: 'm', cadence: 'rpm', power: 'W', temperature: '°C', km: 'km' },
  elapsed: 'Elapsed time', moving: 'Moving time', distance: 'Distance', averageSpeed: 'Average speed', maximumSpeed: 'Maximum speed',
  averagePace: 'Average pace', bestPace: 'Best pace', ascent: 'Ascent', descent: 'Descent',
  averageHeart: 'Average heart rate', maximumHeart: 'Maximum heart rate',
  laps: 'Laps', lap: 'Lap', lapNumber: (/** @type {string} */ n) => `Lap ${n}`, lapTime: 'Time', lapDistance: 'Distance', lapAverageHeart: 'Avg. heart rate',
  lapMaximumHeart: 'Max. heart rate',
};

export let text = tr;
/** @param {string} language */
export function setDetailLanguage(language) {
  text = language === 'en' ? en : tr;
}
/** Both tables, for the completeness test. */
export const tables = { tr, en };
