// The server's own words for a refused request (app/api/*.py, app/services/manual_validation.py),
// each with the dictionary key (t.common.api.known) that says the same in the interface's language.
// They are only recognised here, never shown: the server speaks one language and one register, the
// interface another. A sentence that is not listed falls back to a text chosen by HTTP status.
/** @type {Record<string, string>} */
export const SERVER_MESSAGES = {
  'Girdi geçersiz. Lütfen değerleri kontrol edin.': 'invalidInput',
  'Gelecek ay seçilemez.': 'futureMonth',
  'Gelecek tarih seçilemez.': 'futureDate',
  'Geçerli bir saat dilimi seçin.': 'invalidZone',
  'Metin geçersiz karakter içeriyor.': 'invalidText',
  'Tarih saat dilimi içermeli.': 'invalidDate',
  'Tarih UTC aralığında olmalı.': 'invalidDate',
  'Sayısal değer depolanabilir aralıkta olmalı.': 'numberRange',
  'Spor türü 1–128 karakter olmalı.': 'exerciseName',
  'Bitiş başlangıçtan sonra olmalı ve gelecekte olmamalı.': 'endAfterStart',
  'Kalori ve mesafe geçerli, negatif olmayan değerler olmalı.': 'caloriesDistance',
  'Ölçüm tarihi saat dilimi içermeli ve gelecekte olmamalı.': 'measurementDate',
  'Ölçüm türü geçersiz.': 'measurementType',
};
/** Sentences with values in them: the captured values go to the dictionary function. @type {[RegExp, string][]} */
export const SERVER_PATTERNS = [[/^Ölçüm (\S+)–(\S+) aralığında olmalı\.$/u, 'measurementRange']];
/** Machine codes the server sends as `detail.code`. @type {Record<string, string>} */
export const SERVER_CODES = {
  coverage_invalid_range: 'rangeTooLong',
  garmin_invalid_range: 'rangeTooLong',
  coverage_invalid_timezone: 'invalidZone',
  coverage_unknown_data_type: 'unknownDataType',
};
