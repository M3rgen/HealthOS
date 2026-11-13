// ESLint flat config: eslint:recommended plus no-unsanitized for every HTML sink.
// innerHTML may take a literal, a template whose values all pass through escape()/num(), or a
// call to one of the reviewed markup builders below; any other sink needs an inline
// `eslint-disable-next-line no-unsanitized/property -- <why it is safe>`.
import js from '@eslint/js';
import nounsanitized from 'eslint-plugin-no-unsanitized';
import globals from 'globals';

const markupBuilders = [
  'escape', 'num', // format.js: escaped text, formatted numbers
  'iconMarkup', // icons.js: static SVG strings
  'formatAssistantMessage', // chat-format.js: DOMPurify with a tag/attribute allow-list
  'timeHtml', 'bars', 'line', // connection-panel.js, sparklines.js: escaped dates and numeric SVG
  'writeHtml', 'garminNoteHtml', 'systemDetailsHtml', // connection-panel.js: escaped text and dates
  'coverageMarkup', 'devicesMarkup', 'deviceDetailsMarkup', // reviewed escaped rows; template-escape.test.mjs
  'historyRowsMarkup', 'measurementRowsMarkup', 'measurementMessageMarkup', // escaped day and measurement rows
  'metricHistoryChartMarkup', 'syncRowsMarkup', 'workoutDaysMarkup', // escaped chart, sync and workout values
  'webhookDetailsFragment', // ready nodes built with textContent; no HTML parsing
  'safeHtml.unwrap', // dom-morph.js: private SafeHtml from morphChildren's lint-checked input only
];

// Size limits. They were lowered from 80/12 by extracting real functions; none is met through an
// eslint-disable. Raising one needs a reason in its commit, like any other rule change.
const sizeRules = {
  'max-lines-per-function': ['error', { max: 60, skipBlankLines: true, skipComments: true }],
  complexity: ['error', 10],
  'max-depth': ['error', 4],
  'max-nested-callbacks': ['error', 4],
  'max-params': ['error', 5],
  'max-lines': ['error', { max: 400, skipBlankLines: true, skipComments: true }],
  // GLSL lines are excepted: the shader's bytes are pinned by tests/frontend/body-skin.test.mjs.
  'max-len': ['error', { code: 180, ignorePattern: 'texture2D' }],
};

export default [
  { ignores: ['.test-output/', 'node_modules/'] },
  js.configs.recommended,
  nounsanitized.configs.recommended,
  {
    rules: {
      'no-unsanitized/property': ['error', { escape: { methods: markupBuilders } }],
      // morphChildren (dom-morph.js) writes its second argument into the page, like innerHTML.
      'no-unsanitized/method': ['error', { escape: { methods: markupBuilders } }, { morphChildren: { properties: [1] } }],
    },
  },
  { files: ['src/**/*.js'], languageOptions: { globals: globals.browser }, rules: sizeRules },
  // Translation tables and inline SVG data are text, one entry per line: no function limits apply.
  { files: ['public/assets/boot-guard.js'], languageOptions: { sourceType: 'script', globals: globals.browser }, rules: sizeRules },
  { files: ['src/i18n/*.js'], rules: { 'max-lines': 'off', 'max-len': 'off' } },
  { files: ['src/icons.js'], rules: { 'max-len': 'off' } },
  { files: ['*.js', 'scripts/**/*.mjs'], languageOptions: { globals: globals.node } },
  // Playwright scripts: Node code with page.evaluate() callbacks that run in the browser.
  { files: ['preview/**/*.cjs'], languageOptions: { sourceType: 'commonjs', globals: { ...globals.node, ...globals.browser } } },
];
