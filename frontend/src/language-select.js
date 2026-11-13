// Sistem › Görünüm › Dil. The choice is kept in this browser only; i18n/index.js stores it and
// reloads the page so every module renders again in the chosen language.
import { language, setLanguage } from './i18n/index.js';

/** @param {HTMLSelectElement} [select] */
export function setupLanguageSelect(select = /** @type {HTMLSelectElement} */ (document.getElementById('language'))) {
  select.value = language;
  select.addEventListener('change', () => setLanguage(select.value));
}
