import { defineConfig } from 'vite';
import { cssBreakpoints } from './css-breakpoints.js';
import { performanceBudget } from './build-budget.js';
import { htmlI18n } from './html-i18n.js';
import { thirdPartyLicenses } from './third-party-licenses.js';

export default defineConfig({
  plugins: [htmlI18n(), thirdPartyLicenses(), performanceBudget()],
  css: { postcss: { plugins: [cssBreakpoints()] } },
  build: { outDir: '../app/static', emptyOutDir: true },
  server: { proxy: {
    '/api': { target: 'http://127.0.0.1:8000', ws: true },
    '/health': 'http://127.0.0.1:8000',
    '/sync': 'http://127.0.0.1:8000',
    '/google-health': 'http://127.0.0.1:8000',
  } },
});
