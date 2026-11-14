# Preview harness (dev only)

Screenshots every HealthOS view at the target viewports against a running Vite
dev server, with every backend route answered by **synthetic** fixtures
(`fixtures.cjs`; no account data). It fails when the main page can scroll,
which is the top rule in `CONTRIBUTING.md`. Nothing here is part of `npm run build`.

## Run

```sh
cd frontend
npm install
npx playwright install chromium        # once per machine
npx vite --host 127.0.0.1 --port 5174  # in another terminal; no backend needed
node preview/shoot.cjs --out /tmp/healthos-shots
```

Options:

| flag | default | |
|---|---|---|
| `--out <dir>` | `frontend/.test-output/shoot` | PNGs are written as `<W>x<H>-<lang>-<view>.png` (CSS viewport size, also at 2x) |
| `--base <url>` | `http://127.0.0.1:5174` | dev server to target |
| `--viewports a,b` | `1920x976,1440x900,1280x720,1024x650,768x1024,390x844,375x667,320x700` | |
| `--views a,b` | all | `overview, history-drag, history-open, metric-history, calendar, umay-intro, umay, measurements, measurement-dialog, system, sports, sports-editor`, plus edge cases `appearance`, `appearance-preset` (lane C presets on the dashboard), `appearance-uploaded` (synthetic SVG logo + PNG avatar/background uploads), `notice` (sync error banner), `empty-overview`, `empty-history`, `empty-system`, `empty-sports`, `none-overview`, `none-system` (no source selected: the sample-data offer and the three ways to get data), `sample-overview`, `sample-sports` (sample data loaded: its banner and labels) |
| `--reduced-motion` | on | emulates `prefers-reduced-motion: reduce` |
| `--languages a,b` | `tr,en` | interface languages to shoot |
| `--scale <n>` | `1` | device pixel ratio (1–3); `2` turns 1440x900 into a 2880x1800 PNG |
| `--readme` | off | the README set: views `overview, umay, metric-history, calendar, appearance`, viewports `1440x900,390x844`, English, 2x. Flags after it still override. It also prints a `note:` where the desktop history sheet cuts a row or Umay's answer does not fit; notes do not fail the run |

## Output

A table per view with `document.scrollingElement` `scrollHeight/innerHeight`
and `scrollWidth/innerWidth`. `scrollHeight` counts clipped content too, so an
`overflow:hidden` body does not hide a failure. The exit code is `1` if any
view overflows, a page error is thrown, a backend route is not mocked, or the
page tries to load anything from another origin (remote fonts/CDNs are blocked
and reported). `history-drag` drags the history handle part-way and keeps the
released height; `calendar` is reported as skipped where the date button is
hidden.

## Notes

- The clock is frozen at `NOW` in `fixtures.cjs` (2026-10-16 14:30 UTC), so
  the dates in the screenshots stay the same between runs.
- Umay runs on a mocked WebSocket that streams one Markdown answer.
- Uses Playwright's bundled Chromium in new-headless mode (`channel: 'chromium'`)
  so the WebGL body model renders on the GPU.
- The README images in `docs/media` come from
  `node preview/shoot.cjs --readme --viewports 1440x930,390x844 --out <dir>` (English interface, 2x;
  930 px keeps the last row of the history sheet whole): `1440x930-en-overview.png` is
  `dashboard-desktop.png`, `1440x930-en-umay.png` `umay-chat-desktop.png`,
  `1440x930-en-metric-history.png` `metric-history.png`, `1440x930-en-calendar.png`
  `date-picker.png`, `1440x930-en-appearance.png` `appearance-dialog.png`, `390x844-en-overview.png` `dashboard-mobile.png` and
  `390x844-en-umay.png` `umay-chat-mobile.png`.
- Every day has one value however it is asked for: the dashboard, its past windows and the metric
  history share a seed per date. Umay's answer (`chatAnswer`) and its transfer receipt
  (`chatTransfer`) are computed from the dashboard's last 7 days; Umay answers in Turkish in
  every interface language.
- If the frontend starts calling a new endpoint, add it to `mock()` in
  `shoot.cjs` and give it a fixture.
