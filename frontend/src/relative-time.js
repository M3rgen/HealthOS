// "2 saat önce": a coarse Turkish distance from `now`, for a time whose exact value is shown
// alongside (a tooltip, a details list). Future instants (a clock ahead of the server) read
// as "az önce" rather than a negative age.
const STEPS = [
  [60, 'dk', 60],
  [24, 'saat', 3600],
  [7, 'gün', 86400],
  [5, 'hafta', 604800],
  [12, 'ay', 2629800],
  [Infinity, 'yıl', 31557600],
];

export function relativeTime(value, now = new Date()) {
  if (!value) return null;
  const at = new Date(value);
  if (Number.isNaN(at.getTime())) return null;
  const seconds = Math.max(0, Math.floor((now - at) / 1000));
  if (seconds < 60) return 'az önce';
  for (const [limit, unit, size] of STEPS) {
    const count = Math.floor(seconds / size);
    if (count < limit) return `${count} ${unit} önce`;
  }
  return null;
}
