# Initial load budget

`frontend/performance-budget.json` records a measured baseline, a 5% target margin and the
historical ceiling for every group. The target is `ceil(measured × 1.05)`; the enforced limit is
`min(target, ceiling)`. A capped group does **not** have the full target margin. Ceilings must not
increase without the maintainer's decision.

Measurement (November 11, 2026, Node 26, locked Vite 8.3.1, offline dependencies):

| Group | Measured bytes | Measured + 5% | Historical ceiling | Enforced limit |
| --- | ---: | ---: | ---: | ---: |
| Initial JS gzip | 48,866 | 51,310 | 60,000 | 51,310 |
| Initial CSS gzip | 19,088 | 20,043 | 20,000 | 20,000 (capped) |
| Deferred JS gzip | 166,093 | 174,398 | 170,000 | 170,000 (capped) |
| Deferred CSS gzip | 130 | 137 | 500 | 137 |
| Figure assets | 1,886,128 | 1,980,435 | 1,950,000 | 1,950,000 (capped) |

To update a measurement: run `cd frontend && npm run build`, read the generated
`performance-budget-report.json` in the build output directory (`app/static` by default), then copy `initial.js.gzip`,
`initial.css.gzip`, `deferred.js.gzip`, `deferred.css.gzip` and `figure.bytes` into the corresponding
`measured` fields in `frontend/performance-budget.json`. Review the measurement and computed limit
diff, then run `npm run test:unit` and `npm run test:build` from `frontend`. Keep the 5% target and
historical ceilings unchanged. If a build already exceeds a limit, its error prints the measured
bytes; investigate the growth before changing the baseline. This update is deliberate, never an
automatic part of the build.

`build-budget.js` is a Vite production plugin; it does not depend on an npm script or a fixed output
directory. `npm run build`, `npx vite build`, CI's `--outDir` build and the browser runner's
programmatic build all go through the same check. It follows the static imports from the entry
HTML's module scripts, modulepreload and stylesheet roots, chunk CSS dependencies and local CSS
imports. Shared files are counted once. Images, fonts and source maps are not counted, except the 3D model and textures the deferred code
asks for at run time (`figureAssets`).

The report gives raw and gzip (level 9, per file) bytes separately for each group. On successful
builds the same values are written to `performance-budget-report.json`. Only the gzip totals of the
initial JS and CSS, the deferred JS and CSS and the figure's asset bytes are limited; equal passes,
and exceeding stops the build with an error stating measurement and limit. Body/three.js chunks and
their dependencies are reported separately. If body
or three.js, identified by module id, enters the static startup graph, the build fails regardless
of size.

`npm run test:unit` tests the graph and limits; `npm run test:build` exercises a real Vite build with
a controlled JS or CSS overrun and an alternative output directory. Both are part of `make
test-browser`. Browser tests cover visibility, pending measurements, motion preference, focus and a
working interface on a load error.
