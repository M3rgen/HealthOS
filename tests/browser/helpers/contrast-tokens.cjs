// Colour tokens that are not always on screen in the audited views: the heart and sleep accents
// (also used for small status text such as "Servis hatası" and "Kopuk") and the disabled-button
// colours. Painted text is still audited by auditText; this checks the tokens against every
// surface they are drawn on, including the 11 % status pill tint, with a 5:1 target.
const assert = require('node:assert/strict');
const { ratio, auditText } = require('./contrast.cjs');

const TARGET = 5;

async function auditTokens(page, label) {
  const tokens = await page.evaluate(() => {
    const ctx = document.createElement('canvas').getContext('2d');
    const rgb = value => { ctx.clearRect(0, 0, 1, 1); ctx.fillStyle = value; ctx.fillRect(0, 0, 1, 1); return [...ctx.getImageData(0, 0, 1, 1).data].slice(0, 3); };
    const root = getComputedStyle(document.documentElement);
    const read = name => rgb(root.getPropertyValue(name).trim());
    const names = ['--heart-icon', '--sleep-icon', '--page', '--surface', '--surface-2', '--inset', '--disabled-ink', '--disabled-fill', '--muted'];
    return Object.fromEntries(names.map(name => [name, read(name)]));
  });
  const tint = (fg, bg, alpha) => bg.map((v, i) => fg[i] * alpha + v * (1 - alpha));
  const surfaces = ['--page', '--surface', '--surface-2', '--inset'];
  const failures = [];
  let minimum = Infinity;
  for (const accent of ['--heart-icon', '--sleep-icon']) {
    for (const surface of surfaces) {
      for (const [name, bg] of [[surface, tokens[surface]], [`${surface} pill`, tint(tokens[accent], tokens[surface], .11)]]) {
        const value = ratio(tokens[accent], bg);
        minimum = Math.min(minimum, value);
        if (value < TARGET) failures.push(`${accent} on ${name}: ${value.toFixed(2)}`);
      }
    }
  }
  // Disabled text is exempt from WCAG contrast, but it must stay readable and look different.
  for (const surface of ['--surface', '--surface-2', '--disabled-fill']) {
    const value = ratio(tokens['--disabled-ink'], tokens[surface]);
    if (value < 3) failures.push(`--disabled-ink on ${surface}: ${value.toFixed(2)}`);
  }
  assert.notDeepEqual(tokens['--disabled-ink'], tokens['--muted'], 'disabled text differs from enabled muted text');
  assert.deepEqual(failures, [], `${label}: colour token contrast`);

  // Every disabled button paints with the tokens instead of fading the whole control.
  const disabled = await page.evaluate(() => [...document.querySelectorAll('button:disabled')].map(button => {
    const style = getComputedStyle(button);
    return { name: button.id || button.className || button.textContent.trim(), color: style.color, opacity: Number(style.opacity),
      ink: getComputedStyle(document.documentElement).getPropertyValue('--disabled-ink').trim() };
  }));
  assert.ok(disabled.length, `${label}: no disabled button found to check`);
  const channels = value => value.match(/\d+(\.\d+)?/g).slice(0, 3).map(Number);
  const hex = value => [1, 3, 5].map(i => parseInt(value.slice(i, i + 2), 16));
  for (const button of disabled) {
    assert.deepEqual(channels(button.color), hex(button.ink), `${label}: ${button.name} disabled colour`);
    assert.equal(button.opacity, 1, `${label}: ${button.name} is faded with opacity`);
  }

  // Small status text in the heart accent, painted on its pill: "Servis hatası".
  const saved = await page.evaluate(() => {
    const el = document.getElementById('service-status');
    const saved = [el.className, el.textContent];
    el.className = 'status-text bad'; el.textContent = 'Servis hatası';
    const r = el.getBoundingClientRect(), hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    return { saved, visible: r.width > 0 && r.top >= 0 && r.bottom <= innerHeight && el.contains(hit) };
  });
  try {
    if (saved.visible) await auditText(page, `${label} heart status text`);
    else console.log(`INFO ${label}: service status is off screen here; its token is checked above`);
  } finally {
    await page.evaluate(([className, text]) => {
      const el = document.getElementById('service-status'); el.className = className; el.textContent = text;
    }, saved.saved);
  }
  console.log(`PASS ${label}: accent tokens at least ${minimum.toFixed(2)}:1, ${disabled.length} disabled buttons use the disabled tokens`);
}

module.exports = { auditTokens };
