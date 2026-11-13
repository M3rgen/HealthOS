import { widths } from './src/breakpoints.js';

// A local PostCSS plugin; Vite already provides PostCSS. No dependency or runtime CSS variables.
export function expandBreakpoints(query) {
  return query.replace(/bp\(([\w]+)\)/g, (_match, name) => {
    if (!Object.hasOwn(widths, name)) throw new Error(`Unknown breakpoint: ${name}`);
    return widths[name];
  });
}
export const cssBreakpoints = () => ({
  postcssPlugin: 'healthos-breakpoints',
  AtRule: { media(rule) { rule.params = expandBreakpoints(rule.params); } },
});
