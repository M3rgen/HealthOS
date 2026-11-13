// Every locale-aware formatter of the interface (dates, times, numbers, letter case) follows the
// chosen interface language from here. Technical formats (ISO dates for the API, time zone
// checks) do not depend on the language and use fixed locales where they are built.
import { language } from './i18n/index.js';

export const locale = language === 'en' ? 'en-GB' : 'tr-TR';
/** new Intl.DateTimeFormat in the interface locale. @param {Intl.DateTimeFormatOptions} options */
export const dateFormat = options => new Intl.DateTimeFormat(locale, options);
/** A date-time string in the interface locale (Date#toLocaleString). @param {string|number|Date} value @param {Intl.DateTimeFormatOptions} options */
export const dateTime = (value, options) => new Date(value).toLocaleString(locale, options);
/** A number with the interface's grouping and decimal separator. @param {string|number} value @param {number} [digits] */
export const number = (value, digits = 0) => Number(value).toLocaleString(locale, { maximumFractionDigits: digits });
/** Lower case by the interface language's rules (Turkish İ/ı). @param {unknown} text */
export const lower = text => String(text).toLocaleLowerCase(locale);
