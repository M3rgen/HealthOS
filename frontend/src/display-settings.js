// Sistem › Görünüm: the time zone choice and the body model's rotation. The values live in
// `state` (zoneChoice/zone/motion); the controls only show and change them.
import { $ } from './dom.js';
import { createDisplayPreferences } from './display-preferences.js';
import { t } from './i18n/index.js';
import { state } from './state.js';

const system = t.systemStatus;

/** @param {string | null | undefined} value */
export const validZone = value => {
  if (!value) return false;
  try {
    new Intl.DateTimeFormat('en-US', { timeZone: value });
    return true;
  } catch {
    return false;
  }
};
const deviceZone = (() => {
  try {
    const value = Intl.DateTimeFormat().resolvedOptions().timeZone;
    return validZone(value) ? value : 'UTC';
  } catch {
    return 'UTC';
  }
})();
/** @param {string} choice */
const resolveZone = choice => (choice === 'auto' ? deviceZone : choice);

/**
 * Reads this browser's saved choice into `state` at once, so the first paint is already right.
 * @param {{onZoneChanged: () => unknown, onMotionChanged: () => void}} handlers
 */
export function setupDisplaySettings({ onZoneChanged, onMotionChanged }) {
  const note = $('display-preferences-note');
  const noteText = { saved: note.textContent, saving: system.preferenceSaving, offline: system.preferenceOffline };
  const display = createDisplayPreferences({ validZone, onStatus: status => (note.textContent = noteText[status]) });
  const save = () => display.write({ timezone: state.zoneChoice, motion: state.motion });
  const zoneControl = $('timezone');
  const motionControl = $('model-motion');
  function showZone() {
    if (![...zoneControl.options].some(option => option.value === state.zoneChoice))
      zoneControl.add(new Option(state.zoneChoice, state.zoneChoice));
    zoneControl.value = state.zoneChoice;
  }
  /** @param {string} choice */
  function setZoneChoice(choice) {
    state.zoneChoice = choice;
    state.zone = resolveZone(choice);
  }
  const stored = display.read();
  setZoneChoice(stored.timezone ?? 'auto');
  state.motion = stored.motion ?? true;
  /** @type {HTMLElement} */ (zoneControl.querySelector('option[value="auto"]')).textContent = system.deviceZone(deviceZone);
  showZone();
  motionControl.checked = state.motion;

  zoneControl.addEventListener('change', async () => {
    setZoneChoice(zoneControl.value);
    save();
    await onZoneChanged();
  });
  motionControl.addEventListener('change', () => {
    state.motion = motionControl.checked;
    save();
    onMotionChanged();
  });
  // Preferences saved on another device; null leaves this browser's choice in place.
  /** @param {import('./display-preferences.js').DisplayChoice | null} preferences */
  async function applyRemote(preferences) {
    if (!preferences) return;
    const motion = preferences.motion ?? true;
    if (state.motion !== motion) {
      state.motion = motion;
      motionControl.checked = motion;
      onMotionChanged();
    }
    const choice = preferences.timezone ?? 'auto';
    if (choice === state.zoneChoice) return;
    setZoneChoice(choice);
    showZone();
    await onZoneChanged();
  }
  return { applyRemote, sync: () => display.sync() };
}
