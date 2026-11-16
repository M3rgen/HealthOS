// Loaded with the Plan page, including translations, outside the first-render graph.

const tr = {
  meal: 'Öğün', content: 'İçerik', today: 'Bugün', nutrition: 'Beslenme', batch: 'Toplu pişirme', shopping: 'Alışveriş', training: 'Antrenman',
  measurements: 'Ölçüm', assumptions: 'Varsayımlar', sections: 'Plan bölümleri',
  empty: 'Henüz plan yok', emptyDetail: 'Özel plan belgeni yerel dosyadan içe aktar. Ayrıntılar: docs/reference/plan.md',
  noSections: 'Bu belgede henüz gösterilecek bölüm yok.', loading: 'Plan yükleniyor…', failed: 'Plan alınamadı.',
  retry: 'Yeniden dene', saveFailed: 'Plan durumu kaydedilemedi. Kaydedilmeyen değişiklikler geri alındı.',
  saved: 'Plan durumu kaydedildi.', saving: 'Kaydediliyor…', otherDay: 'Başka günü göster', start: 'Program başlangıcı (haftalar pazartesiden sayılır)',
  noDay: 'Bu gün için öğün tanımlanmamış.', noTraining: 'Bu gün için antrenman tanımlanmamış.',
  week: 'Program haftası', notStarted: 'Program henüz başlamadı.', noStart: 'Program haftası için başlangıç tarihini ayarla.',
  total: 'Toplam', protein: 'Protein', estimated: 'Tahmini toplam', oneOff: 'Tek seferlik alışveriş',
  reset: 'Listeyi sıfırla', confirmReset: 'Alışveriş listesindeki tüm işaretler kaldırılsın mı?',
  ingredients: 'Malzemeler', preparation: 'Yapılışı', storage: 'Saklama kuralları', substitutions: 'Değiştirme kuralları',
  adjustment: 'Ayar kuralları', notes: 'Notlar', rules: 'Kurallar', schedule: 'Haftalık düzen',
  movement: 'Hareket', sets: 'Set / tekrar', note: 'Not', weeks: 'Hafta', steps: 'Adımlar', duration: 'Süre',
  addMeasurement: 'Ölçüm ekle', current: 'İçinde bulunulan hafta', missingValues: 'Toplamlar yalnız belirtilen değerleri içerir.',
};
const en = {
  meal: 'Meal', content: 'Content', today: 'Today', nutrition: 'Nutrition', batch: 'Batch cooking', shopping: 'Shopping', training: 'Training',
  measurements: 'Measurements', assumptions: 'Assumptions', sections: 'Plan sections',
  empty: 'No plan yet', emptyDetail: 'Import your private plan document from a local file. Details: docs/reference/plan.md',
  noSections: 'This document has no sections to display yet.', loading: 'Loading plan…', failed: 'Could not load the plan.',
  retry: 'Try again', saveFailed: 'Could not save plan state. Unsaved changes were rolled back.',
  saved: 'Plan state saved.', saving: 'Saving…', otherDay: 'Show another day', start: 'Program start (weeks count from Monday)',
  noDay: 'No meals are defined for this day.', noTraining: 'No training is defined for this day.',
  week: 'Program week', notStarted: 'The program has not started yet.', noStart: 'Set a start date to see the program week.',
  total: 'Total', protein: 'Protein', estimated: 'Estimated total', oneOff: 'One-off purchases',
  reset: 'Reset list', confirmReset: 'Clear all check marks in the shopping list?',
  ingredients: 'Ingredients', preparation: 'Preparation', storage: 'Storage rules', substitutions: 'Substitution rules',
  adjustment: 'Adjustment rules', notes: 'Notes', rules: 'Rules', schedule: 'Weekly schedule',
  movement: 'Movement', sets: 'Sets / reps', note: 'Note', weeks: 'Week', steps: 'Steps', duration: 'Duration',
  addMeasurement: 'Add measurement', current: 'Current week', missingValues: 'Totals include only the values provided.',
};
export let text = tr;
/** @param {string} language */
export function setPlanLanguage(language) {
  text = language === 'en' ? en : tr;
}
